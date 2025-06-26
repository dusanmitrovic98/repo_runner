from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import os
import subprocess
import threading

# Load environment variables from .env file
load_dotenv()

SECRET_KEY = os.getenv('SECRET_KEY', 'changeme')
PORT = int(os.getenv('PORT', 5000))

app = Flask(__name__)
app.secret_key = SECRET_KEY
socketio = SocketIO(app)

process_lock = threading.Lock()
running_process = {'proc': None}

def kill_running_process():
    with process_lock:
        proc = running_process['proc']
        if proc and proc.poll() is None:
            try:
                proc.terminate()
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
    command = data.get('command')
    if not command:
        emit('output', {'output': '\nNo command provided.'})
        return
    kill_running_process()  # Stop any previous process
    emit('output', {'output': f"\n$ {command}\n"})
    try:
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
    socketio.run(app, host='0.0.0.0', port=PORT)
