from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import os
import subprocess
import threading
import signal  # Added for sending SIGINT
import sys  # Added for platform check
import time
import psutil  # Add this new import
import atexit  # Add this import at the top
from collections import deque  # Add this import for output buffering
import json  # Add this import for JSON persistence

# Load environment variables from .env file
load_dotenv()

SECRET_KEY = os.getenv('SECRET_KEY', 'changeme')
PORT = int(os.getenv('PORT', 5001))

app = Flask(__name__)
app.secret_key = SECRET_KEY
socketio = SocketIO(app)

# Add output buffer to each session (store last 100 lines)
OUTPUT_BUFFER_SIZE = 100

DATA_DIR = 'data'
SESSION_DATA_DIR = os.path.join(DATA_DIR, 'session_data')
os.makedirs(SESSION_DATA_DIR, exist_ok=True)

process_lock = threading.Lock()
running_processes = {}  # session_id -> {'proc': ..., 'cwd': ..., 'output_buffer': deque}

def kill_running_process(session_id):
    with process_lock:
        proc_info = running_processes.get(session_id)
        if proc_info and proc_info['proc'] and proc_info['proc'].poll() is None:
            try:
                parent = psutil.Process(proc_info['proc'].pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                time.sleep(0.5)
                for child in children:
                    try:
                        if child.is_running():
                            child.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
            finally:
                running_processes[session_id]['proc'] = None

def cleanup_all_processes():
    with process_lock:
        for session_id, proc_info in running_processes.items():
            if proc_info['proc'] and proc_info['proc'].poll() is None:
                try:
                    parent = psutil.Process(proc_info['proc'].pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    time.sleep(0.5)
                    for child in children:
                        try:
                            if child.is_running():
                                child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                except psutil.NoSuchProcess:
                    pass
                finally:
                    proc_info['proc'] = None

atexit.register(cleanup_all_processes)

def handle_exit_signal(signum, frame):
    cleanup_all_processes()
    os._exit(0)

# Register signal handlers for SIGINT and SIGTERM
signal.signal(signal.SIGINT, handle_exit_signal)
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, handle_exit_signal)

# Update load/save session buffer to handle list of dicts

def load_session_buffer(session_id):
    path = os.path.join(SESSION_DATA_DIR, f'{session_id}.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # If old format, convert to new
            buf = data.get('output_buffer', [])
            if buf and isinstance(buf[0], str):
                # Convert to new format
                buf = [{'cwd': data.get('cwd', os.getcwd()), 'command': '', 'output': line} for line in buf]
            return deque(buf, maxlen=OUTPUT_BUFFER_SIZE), data.get('cwd', os.getcwd())
    return deque(maxlen=OUTPUT_BUFFER_SIZE), os.getcwd()

def save_session_buffer(session_id, output_buffer, cwd):
    path = os.path.join(SESSION_DATA_DIR, f'{session_id}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({
            'output_buffer': list(output_buffer),
            'cwd': cwd
        }, f)

# Update handle_reconnect_session to replay history with correct cwd/command
@socketio.on('reconnect_session')
def handle_reconnect_session(data):
    print('[SocketIO] Event: reconnect_session', data, flush=True)
    session_id = data.get('session_id')
    if not session_id:
        emit('output', {'output': '\n[Error: No session_id provided]\n'})
        return
    with process_lock:
        proc_info = running_processes.get(session_id)
        if not proc_info:
            # Load from disk if present
            output_buffer, cwd = load_session_buffer(session_id)
            running_processes[session_id] = {'proc': None, 'cwd': cwd, 'output_buffer': output_buffer}
            proc_info = running_processes[session_id]
        # Send buffered output
        output_buffer = proc_info.get('output_buffer', deque())
        for entry in output_buffer:
            if isinstance(entry, dict):
                prompt = f"\n{entry['cwd']}\n$ {entry['command']}\n" if entry['command'] else ''
                if prompt:
                    emit('command_output', {'output': prompt, 'session_id': session_id})
                if entry['output']:
                    emit('command_output', {'output': entry['output'], 'session_id': session_id})
            else:
                emit('command_output', {'output': entry, 'session_id': session_id})
        # Send current working directory
        emit('session_state', {'cwd': proc_info['cwd'], 'session_id': session_id})
        # Send process status
        running = proc_info['proc'] is not None and proc_info['proc'].poll() is None
        emit('process_status', {'running': running, 'session_id': session_id})

@app.route('/', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('secret_key') == SECRET_KEY:
            session['logged_in'] = True
            return redirect(url_for('terminal'))
        else:
            error = 'Invalid secret key.'
    return render_template('login.html', error=error)

@app.route('/terminal')
def terminal():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('terminal.html')

@socketio.on('run_command')
def handle_run_command(data):
    print('[SocketIO] Event: run_command', data, flush=True)
    session_id = data.get('session_id')
    if not session_id:
        emit('output', {'output': '\n[Error: No session_id provided]\n'})
        return
    command = data.get('command')
    if not command:
        emit('output', {'output': '\nNo command provided.', 'session_id': session_id})
        return
    # Get or initialize session state
    with process_lock:
        if session_id not in running_processes:
            output_buffer, cwd = load_session_buffer(session_id)
            running_processes[session_id] = {'proc': None, 'cwd': cwd, 'output_buffer': output_buffer}
        proc_info = running_processes[session_id]
        if 'output_buffer' not in proc_info:
            proc_info['output_buffer'] = deque(maxlen=OUTPUT_BUFFER_SIZE)
    # Handle 'cls' and 'clear' commands
    if command.strip() in ['cls', 'clear']:
        print('[Terminal] Handling clear/cls command', flush=True)
        proc_info['output_buffer'] = deque(maxlen=OUTPUT_BUFFER_SIZE)  # Reset buffer to empty deque
        # Force overwrite the session file with empty buffer
        path = os.path.join(SESSION_DATA_DIR, f'{session_id}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'output_buffer': [], 'cwd': proc_info['cwd']}, f)
        # Reload buffer from disk to ensure in-memory and file are in sync
        proc_info['output_buffer'], _ = load_session_buffer(session_id)
        print(f'[Terminal] Buffer after clear: {list(proc_info["output_buffer"])}', flush=True)
        emit('clear_output', {'session_id': session_id})
        socketio.emit('process_stopped', {'session_id': session_id})
        return
    # Handle 'cd' command specially
    if command.strip().startswith('cd '):
        print('[Terminal] Handling cd command:', command, flush=True)
        parts = command.strip().split(maxsplit=1)
        if len(parts) == 2:
            new_dir = parts[1].strip('"')
            try:
                new_path = os.path.abspath(os.path.join(proc_info['cwd'], new_dir))
                if os.path.isdir(new_path):
                    proc_info['cwd'] = new_path
                    msg = f'Changed directory to {proc_info["cwd"]}\n'
                    proc_info['output_buffer'].append(msg)
                    emit('output', {'output': msg, 'session_id': session_id})
                else:
                    msg = f'No such directory: {new_dir}\n'
                    proc_info['output_buffer'].append(msg)
                    emit('output', {'output': msg, 'session_id': session_id})
            except Exception as e:
                msg = f'Error changing directory: {e}\n'
                proc_info['output_buffer'].append(msg)
                emit('output', {'output': msg, 'session_id': session_id})
        else:
            msg = 'Usage: cd <directory>\n'
            proc_info['output_buffer'].append(msg)
            emit('output', {'output': msg, 'session_id': session_id})
        save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd'])
        socketio.emit('process_stopped', {'session_id': session_id})  # Ensure frontend is reset
        return
    # Only run this for real shell commands
    else:
        print('[Terminal] Running command:', command, 'in', proc_info['cwd'], flush=True)
        kill_running_process(session_id)  # Stop any previous process for this session
        # Store each command as a dict with cwd, command, and output
        entry = {'cwd': proc_info['cwd'], 'command': command, 'output': ''}
        proc_info['output_buffer'].append(entry)
        # Emit the prompt line with cwd and command
        prompt = f"\n{entry['cwd']}\n$ {entry['command']}\n"
        emit('command_output', {'output': prompt, 'session_id': session_id})
        save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd'])
        try:
            if os.name == 'nt':  # Windows
                proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=proc_info['cwd'], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:  # Unix
                proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=proc_info['cwd'], preexec_fn=os.setsid)
            with process_lock:
                running_processes[session_id]['proc'] = proc
            for line in proc.stdout:
                print('[Terminal] Output:', line.strip(), flush=True)
                entry['output'] += line
                emit('command_output', {'output': line, 'session_id': session_id})
                save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd'])
            proc.wait()
        except Exception as e:
            msg = f'Error: {e}\n'
            print('[Terminal] Exception:', e, flush=True)
            entry['output'] += msg
            emit('command_output', {'output': msg, 'session_id': session_id})
            save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd'])
        finally:
            with process_lock:
                running_processes[session_id]['proc'] = None
            socketio.emit('process_stopped', {'session_id': session_id})
            save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd'])

@socketio.on('stop_command')
def handle_stop_command(data):
    session_id = data.get('session_id')
    if not session_id:
        return
    kill_running_process(session_id)
    # Remove session data file on stop
    path = os.path.join(SESSION_DATA_DIR, f'{session_id}.json')
    if os.path.exists(path):
        os.remove(path)
    socketio.emit('command_output', {'output': '\n[Process stopped]\n', 'session_id': session_id})
    socketio.emit('process_stopped', {'session_id': session_id})

@socketio.on('send_command')
def handle_send_command(data):
    handle_run_command(data)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
