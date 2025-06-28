from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import os
import threading
import signal
import time
import asyncio
import atexit
import re
import json
from collections import deque
from database import db
from flask import jsonify

# Load environment variables from .env file
load_dotenv()

SECRET_KEY = os.getenv('SECRET_KEY', 'changeme')
PORT = int(os.getenv('PORT', 5001))

app = Flask(__name__)
app.secret_key = SECRET_KEY
socketio = SocketIO(app)

OUTPUT_BUFFER_SIZE = 100

process_lock = threading.Lock()
running_processes = {}  # session_id -> {'proc': ..., 'cwd': ..., 'output_buffer': deque}

def kill_running_process(session_id):
    with process_lock:
        proc_info = running_processes.get(session_id)
        proc = proc_info['proc'] if proc_info else None
        if proc and proc.poll() is None:
            try:
                if os.name == 'nt':
                    try:
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.5)
                    except Exception:
                        pass
                    try:
                        proc.terminate()
                        time.sleep(0.5)
                    except Exception:
                        pass
                    try:
                        proc.kill()
                    except Exception:
                        pass
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        time.sleep(0.5)
                    except Exception:
                        pass
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                try:
                    import psutil
                    parent = psutil.Process(proc.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    time.sleep(0.5)
                    for child in children:
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                running_processes[session_id]['proc'] = None

def cleanup_all_processes():
    with process_lock:
        for session_id, proc_info in running_processes.items():
            if proc_info['proc'] and proc_info['proc'].poll() is None:
                try:
                    import psutil
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
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                except Exception:
                    pass
                finally:
                    proc_info['proc'] = None

atexit.register(cleanup_all_processes)

def handle_exit_signal(signum, frame):
    cleanup_all_processes()
    os._exit(0)

signal.signal(signal.SIGINT, handle_exit_signal)
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, handle_exit_signal)

# --- DB-backed session buffer ---
async def db_load_session_buffer(session_id):
    data = await db.get("session_buffers", {"session_id": session_id})
    if data:
        buf = data.get('output_buffer', [])
        if buf and isinstance(buf, list) and buf and isinstance(buf[0], str):
            buf = [{'cwd': data.get('cwd', os.getcwd()), 'command': '', 'output': line} for line in buf]
        return deque(buf, maxlen=OUTPUT_BUFFER_SIZE), data.get('cwd', os.getcwd())
    return deque(maxlen=OUTPUT_BUFFER_SIZE), os.getcwd()

async def db_save_session_buffer(session_id, output_buffer, cwd):
    await db.update("session_buffers", {"session_id": session_id}, {
        "session_id": session_id,
        "output_buffer": list(output_buffer),
        "cwd": cwd
    }, upsert=True)

async def db_clear_session_buffer(session_id, cwd):
    await db.update("session_buffers", {"session_id": session_id}, {
        "session_id": session_id,
        "output_buffer": [],
        "cwd": cwd
    }, upsert=True)

async def db_delete_session_buffer(session_id):
    await db.delete("session_buffers", {"session_id": session_id})

# --- SocketIO handlers ---
@socketio.on('reconnect_session')
def handle_reconnect_session(data):
    print('[SocketIO] Event: reconnect_session', data, flush=True)
    session_id = data.get('session_id')
    if not session_id:
        emit('output', {'output': '\n[Error: No session_id provided]\n'})
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    output_buffer, cwd = loop.run_until_complete(db_load_session_buffer(session_id))
    with process_lock:
        running_processes[session_id] = {'proc': None, 'cwd': cwd, 'output_buffer': output_buffer}
        proc_info = running_processes[session_id]
    if not output_buffer or len(output_buffer) == 0:
        prompt = f"\n{proc_info['cwd']}\n$ "
        emit('command_output', {'output': prompt, 'session_id': session_id})
    for entry in output_buffer:
        if isinstance(entry, dict):
            prompt = f"\n{entry['cwd']}\n$ {entry['command']}\n" if entry['command'] else ''
            if prompt:
                emit('command_output', {'output': prompt, 'session_id': session_id})
            if entry['output']:
                emit('command_output', {'output': entry['output'], 'session_id': session_id})
        else:
            emit('command_output', {'output': entry, 'session_id': session_id})
    emit('session_state', {'cwd': proc_info['cwd'], 'session_id': session_id})
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

def strip_ansi_codes(text):
    ansi_escape = re.compile(r'\x1B\[[0-9;?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with process_lock:
        if session_id not in running_processes:
            output_buffer, cwd = loop.run_until_complete(db_load_session_buffer(session_id))
            running_processes[session_id] = {'proc': None, 'cwd': cwd, 'output_buffer': output_buffer}
        proc_info = running_processes[session_id]
        if 'output_buffer' not in proc_info:
            proc_info['output_buffer'] = deque(maxlen=OUTPUT_BUFFER_SIZE)
        if not proc_info['output_buffer'] or len(proc_info['output_buffer']) == 0:
            emit('clear_output', {'session_id': session_id})
    if command.strip() in ['cls', 'clear']:
        print('[Terminal] Handling clear/cls command', flush=True)
        proc_info['output_buffer'] = deque(maxlen=OUTPUT_BUFFER_SIZE)
        loop.run_until_complete(db_clear_session_buffer(session_id, proc_info['cwd']))
        proc_info['output_buffer'], _ = loop.run_until_complete(db_load_session_buffer(session_id))
        print(f'[Terminal] Buffer after clear: {list(proc_info["output_buffer"])}', flush=True)
        emit('clear_output', {'session_id': session_id})
        prompt = f"\n{proc_info['cwd']}\n$ "
        emit('command_output', {'output': prompt, 'session_id': session_id})
        socketio.emit('process_stopped', {'session_id': session_id})
        return
    if command.strip().startswith('cd '):
        print('[Terminal] Handling cd command:', command, flush=True)
        parts = command.strip().split(maxsplit=1)
        if len(parts) == 2:
            new_dir = parts[1].strip('"')
            try:
                new_path = os.path.abspath(os.path.join(proc_info['cwd'], new_dir))
                if os.path.isdir(new_path):
                    proc_info['cwd'] = new_path
                    msg = f''
                    proc_info['output_buffer'].append({'cwd': proc_info['cwd'], 'command': command, 'output': msg})
                    emit('command_output', {'output': f"\n{proc_info['cwd']}\n$ {command}\n", 'session_id': session_id})
                    emit('command_output', {'output': msg, 'session_id': session_id})
                else:
                    msg = f'No such directory: {new_dir}\n'
                    proc_info['output_buffer'].append({'cwd': proc_info['cwd'], 'command': command, 'output': msg})
                    emit('command_output', {'output': f"\n{proc_info['cwd']}\n$ {command}\n", 'session_id': session_id})
                    emit('command_output', {'output': msg, 'session_id': session_id})
            except Exception as e:
                msg = f'Error changing directory: {e}\n'
                proc_info['output_buffer'].append({'cwd': proc_info['cwd'], 'command': command, 'output': msg})
                emit('command_output', {'output': f"\n{proc_info['cwd']}\n$ {command}\n", 'session_id': session_id})
                emit('command_output', {'output': msg, 'session_id': session_id})
        else:
            msg = 'Usage: cd <directory>\n'
            proc_info['output_buffer'].append({'cwd': proc_info['cwd'], 'command': command, 'output': msg})
            emit('command_output', {'output': f"\n{proc_info['cwd']}\n$ {command}\n", 'session_id': session_id})
            emit('command_output', {'output': msg, 'session_id': session_id})
        loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))
        socketio.emit('process_stopped', {'session_id': session_id})
        return
    else:
        print('[Terminal] Running command:', command, 'in', proc_info['cwd'], flush=True)
        kill_running_process(session_id)
        entry = {'cwd': proc_info['cwd'], 'command': command, 'output': ''}
        proc_info['output_buffer'].append(entry)
        prompt = f"\n{entry['cwd']}\n$ {entry['command']}\n"
        emit('command_output', {'output': prompt, 'session_id': session_id})
        loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))
        try:
            import subprocess
            if os.name == 'nt':
                proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=proc_info['cwd'], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, bufsize=1, universal_newlines=True)
            else:
                proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=proc_info['cwd'], preexec_fn=os.setsid, bufsize=1, universal_newlines=True)
            with process_lock:
                running_processes[session_id]['proc'] = proc
            socketio.emit('process_status', {'running': True, 'session_id': session_id})
            for line in proc.stdout:
                line = strip_ansi_codes(line)
                print('[Terminal] Output:', line.strip(), flush=True)
                entry['output'] += line
                emit('command_output', {'output': line, 'session_id': session_id})
                loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))
            proc.wait()
            loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))
        except Exception as e:
            msg = f'Error: {e}\n'
            print('[Terminal] Exception:', e, flush=True)
            entry['output'] += msg
            emit('command_output', {'output': msg, 'session_id': session_id})
            loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))
        finally:
            with process_lock:
                running_processes[session_id]['proc'] = None
            socketio.emit('process_stopped', {'session_id': session_id})
            socketio.emit('process_status', {'running': False, 'session_id': session_id})
            loop.run_until_complete(db_save_session_buffer(session_id, proc_info['output_buffer'], proc_info['cwd']))

@socketio.on('stop_command')
def handle_stop_command(data):
    session_id = data.get('session_id')
    if not session_id:
        return
    kill_running_process(session_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db_delete_session_buffer(session_id))
    socketio.emit('command_output', {'output': '\n[Process stopped]\n', 'session_id': session_id})
    socketio.emit('process_stopped', {'session_id': session_id})
    socketio.emit('process_status', {'running': False, 'session_id': session_id})

@socketio.on('send_command')
def handle_send_command(data):
    handle_run_command(data)

def get_user_key():
    return session.get('user_id') or session.get('sid') or session.get('logged_in') or 'anon'

@app.route('/api/terminal_state', methods=['GET'])
def api_get_terminal_state():
    user_key = get_user_key()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state = loop.run_until_complete(db.get("terminal_states", {"user_key": user_key}))
    return jsonify(state or {})

@app.route('/api/terminal_state', methods=['POST'])
def api_set_terminal_state():
    user_key = get_user_key()
    state = request.json
    state['user_key'] = user_key
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db.update("terminal_states", {"user_key": user_key}, state, upsert=True))
    return jsonify({"success": True})

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    socketio.run(app, host='0.0.0.0', port=PORT)
