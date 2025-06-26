from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import os
import subprocess
import threading
import signal  # Added for sending SIGINT

# Load environment variables from .env file
load_dotenv()

SECRET_KEY = os.getenv('SECRET_KEY', 'changeme')
PORT = int(os.getenv('PORT', 5001))

app = Flask(__name__)
app.secret_key = SECRET_KEY
socketio = SocketIO(app)

process_lock = threading.Lock()
running_process = {'proc': None}
current_cwd = os.getcwd()  # Track current working directory

def kill_running_process():
    with process_lock:
        proc = running_process['proc']
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)  # Send SIGINT (Ctrl+C)
            except Exception:
                pass
            running_process['proc'] = None

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
    global current_cwd
    command = data.get('command')
    if not command:
        emit('output', {'output': '\nNo command provided.'})
        return
    # Handle 'cd' command specially
    if command.strip().startswith('cd '):
        parts = command.strip().split(maxsplit=1)
        if len(parts) == 2:
            new_dir = parts[1].strip('"')
            try:
                new_path = os.path.abspath(os.path.join(current_cwd, new_dir))
                if os.path.isdir(new_path):
                    current_cwd = new_path
                    emit('output', {'output': f'Changed directory to {current_cwd}\n'})
                else:
                    emit('output', {'output': f'No such directory: {new_dir}\n'})
            except Exception as e:
                emit('output', {'output': f'Error changing directory: {e}\n'})
        else:
            emit('output', {'output': 'Usage: cd <directory>\n'})
        return
    kill_running_process()  # Stop any previous process
    emit('output', {'output': f"\n$ {command}\n"})
    try:
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=current_cwd)
        with process_lock:
            running_process['proc'] = proc
        for line in proc.stdout:
            socketio.emit('output', {'output': line})
        proc.wait()
    except Exception as e:
        emit('output', {'output': f'Error: {e}\n'})
    finally:
        with process_lock:
            running_process['proc'] = None
        socketio.emit('process_stopped')

@socketio.on('stop_command')
def handle_stop_command():
    kill_running_process()
    emit('output', {'output': '\n[Process stopped]\n'})
    socketio.emit('process_stopped')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
