import os
import subprocess
import shutil
import threading
import time
import json
import sys
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psutil
from werkzeug.exceptions import HTTPException

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# --- Session-based Authentication ---
@app.before_request
def require_secret_key():
    # Allow static files and login
    if request.endpoint in ['static', 'login']:
        return
    if not session.get('authenticated', False):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        key = request.form.get('secret_key', '')
        if key and key == os.environ.get('SECRET_KEY', 'dev-secret'):
            session['authenticated'] = True
            return redirect(url_for('index'))
        else:
            error = 'Invalid secret key.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# Configuration
REPOS_DIR = os.path.join(os.getcwd(), 'repos')
VENVS_DIR = os.path.join(os.getcwd(), 'venvs')
CONFIG_FILE = os.path.join(os.getcwd(), 'projects.json')

# Process management
current_process = None
process_lock = threading.Lock()
process_info = {
    "status": "stopped",
    "start_time": None,
    "pid": None,
    "project": None
}

# Output management
output_buffer = []
output_lock = threading.Lock()

# Logging configuration
def configure_logging():
    log_formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s [in %(pathname)s:%(lineno)d]'
    )
    
    file_handler = RotatingFileHandler('controller.log', maxBytes=100000, backupCount=3)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG)
    
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.setLevel(logging.DEBUG)

configure_logging()

# --- Project Config and Name Utilities ---
def get_project_name(repo_url):
    """
    Extracts a project name from the repo URL or path.
    E.g. https://github.com/user/repo.git -> repo
    """
    name = repo_url.rstrip('/').split('/')[-1]
    if name.endswith('.git'):
        name = name[:-4]
    return name

def load_project_config(project_name):
    """
    Loads the project config from the CONFIG_FILE (projects.json).
    Returns a dict, or an empty dict if not found.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            all_configs = json.load(f)
        return all_configs.get(project_name, {})
    except Exception:
        return {}

def save_project_config(project_name, config):
    """
    Saves the project config to the CONFIG_FILE (projects.json).
    """
    all_configs = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                all_configs = json.load(f)
        except Exception:
            all_configs = {}
    all_configs[project_name] = config
    with open(CONFIG_FILE, 'w') as f:
        json.dump(all_configs, f, indent=2)

# Utility: Run a shell command, optionally in the project's venv
def get_venv_path(project_name):
    return os.path.join(VENVS_DIR, project_name)

def get_repo_dir(project_name):
    return os.path.join(REPOS_DIR, project_name)

def venv_exists(project_name):
    venv_path = get_venv_path(project_name)
    if os.name == 'nt':
        return os.path.exists(os.path.join(venv_path, 'Scripts', 'activate'))
    else:
        return os.path.exists(os.path.join(venv_path, 'bin', 'activate'))

def run_command(cmd, project_name=None, cwd=None, env=None, use_venv=False, background=False):
    """
    Run a shell command, optionally activating the venv for the project.
    Returns a subprocess.CompletedProcess (or Popen if background=True).
    """
    venv_path = get_venv_path(project_name) if (use_venv and project_name) else None
    if env is None:
        env = os.environ.copy()
    if cwd is None and project_name:
        cwd = get_repo_dir(project_name)

    if use_venv and venv_path:
        if os.name == 'nt':
            # Windows: use venv\Scripts\activate.bat
            activate = os.path.join(venv_path, 'Scripts', 'activate.bat')
            full_cmd = f'call "{activate}" && {cmd}'
            shell = True
        else:
            # Unix: use . venv/bin/activate (not source)
            activate = os.path.join(venv_path, 'bin', 'activate')
            full_cmd = f'. "{activate}" && {cmd}'
            shell = True
    else:
        full_cmd = cmd
        shell = True

    if background:
        # For background processes, use Popen
        global current_process, process_info
        with process_lock:
            current_process = subprocess.Popen(full_cmd, shell=shell, cwd=cwd, env=env)
            process_info["status"] = "running"
            process_info["start_time"] = time.time()
            process_info["pid"] = current_process.pid
            process_info["project"] = project_name
        return current_process
    else:
        result = subprocess.run(full_cmd, shell=shell, cwd=cwd, env=env, capture_output=True, text=True)
        return result

# --- Single Project Mode Utilities ---
def get_single_project():
    """
    Returns the only project in the repos directory, or None if not found.
    """
    if os.path.exists(REPOS_DIR):
        projects = [name for name in os.listdir(REPOS_DIR)
                    if os.path.isdir(os.path.join(REPOS_DIR, name))]
        if projects:
            return projects[0]
    return None

# --- Routes (Single Project Mode) ---
@app.route('/')
def index():
    projects = []
    if os.path.exists(REPOS_DIR):
        projects = [name for name in os.listdir(REPOS_DIR) 
                   if os.path.isdir(os.path.join(REPOS_DIR, name))]
    
    active_project = session.get('active_project', projects[0] if projects else None)
    config = load_project_config(active_project) if active_project else {}
    
    return render_template('index.html', 
        projects=projects,
        active_project=active_project,
        repo_url=config.get('repo_url', ''),
        build_cmd=config.get('build_cmd', ''),
        start_cmd=config.get('start_cmd', ''),
        env_vars=config.get('env_vars', '')
    )

@app.route('/clone', methods=['POST'])
def clone_repo():
    repo_url = request.form.get('repo_url')
    if not repo_url:
        return jsonify({'error': 'Repository URL required'}), 400
    
    if not (repo_url.startswith('http') or '@' in repo_url):
        return jsonify({'error': 'Invalid repository URL'}), 400
    
    try:
        project_name = get_project_name(repo_url)
        repo_dir = get_repo_dir(project_name)
        
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir)
        
        result = run_command(f'git clone {repo_url} {repo_dir}', 
                            project_name, 
                            cwd=os.getcwd(),
                            use_venv=False)
        
        if result.returncode != 0:
            return jsonify({'error': f'Clone failed: {result.stderr}'}), 500
        
        session['active_project'] = project_name
        save_project_config(project_name, {
            'repo_url': repo_url,
            'build_cmd': '',
            'start_cmd': '',
            'env_vars': ''
        })
        
        return jsonify({
            'message': 'Repository cloned successfully',
            'project_name': project_name
        })
    
    except Exception as e:
        app.logger.error(f"Clone error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/delete_repo', methods=['POST'])
def delete_repo():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to delete'}), 400
    try:
        repo_dir = get_repo_dir(project_name)
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir)
        session.pop('active_project', None)
        return jsonify({'message': f'Repository {project_name} deleted'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/recreate_venv', methods=['POST'])
def recreate_venv():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to recreate venv for'}), 400
    try:
        venv_path = get_venv_path(project_name)
        if os.path.exists(venv_path):
            shutil.rmtree(venv_path)
        python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        result = run_command(
            f'{python_version} -m venv {venv_path}',
            project_name,
            cwd=os.getcwd(),
            use_venv=False
        )
        if result.returncode != 0:
            return jsonify({'error': f'Virtual environment creation failed: {result.stderr}'}), 500
        req_file = os.path.join(get_repo_dir(project_name), 'requirements.txt')
        if os.path.exists(req_file):
            pip_cmd = f'pip install -r {req_file}'
            result = run_command(pip_cmd, project_name, use_venv=True)
            if result.returncode != 0:
                return jsonify({'error': f'Dependency install failed: {result.stderr}'}), 500
        return jsonify({'message': f'Virtual environment for {project_name} recreated'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/build', methods=['POST'])
def run_build():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to build'}), 400
    build_cmd = request.form.get('build_cmd')
    if not build_cmd:
        return jsonify({'error': 'Build command required'}), 400
    try:
        config = load_project_config(project_name)
        config['build_cmd'] = build_cmd
        save_project_config(project_name, config)
        result = run_command(build_cmd, project_name, use_venv=venv_exists(project_name))
        if result.returncode != 0:
            return jsonify({'error': f'Build failed: {result.stderr}'}), 500
        return jsonify({'message': 'Build completed', 'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/start', methods=['POST'])
def run_start():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to start'}), 400
    start_cmd = request.form.get('start_cmd')
    if not start_cmd:
        return jsonify({'error': 'Start command required'}), 400
    try:
        config = load_project_config(project_name)
        config['start_cmd'] = start_cmd
        save_project_config(project_name, config)
        env = {}
        env_file = os.path.join(get_repo_dir(project_name), '.env')
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.strip() and not line.startswith('#'):
                        key, value = line.strip().split('=', 1)
                        env[key] = value
        run_command(start_cmd, project_name, background=True, env=env, use_venv=venv_exists(project_name))
        return jsonify({'message': 'Application started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stop', methods=['POST'])
def stop_app():
    global current_process, process_info
    with process_lock:
        if current_process:
            current_process.terminate()
            try:
                current_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                current_process.kill()
            current_process = None
            process_info = {"status": "stopped", "project": None}
    return jsonify({'message': 'Application stopped'})

@app.route('/pull', methods=['POST'])
def pull_updates():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to pull'}), 400
    try:
        result = run_command('git pull', project_name, use_venv=venv_exists(project_name))
        if result.returncode != 0:
            return jsonify({'error': f'Pull failed: {result.stderr}'}), 500
        return jsonify({'message': 'Updates pulled', 'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/save_env', methods=['POST'])
def save_env():
    project_name = session.get('active_project') or get_single_project()
    if not project_name:
        return jsonify({'error': 'No project to save env for'}), 400
    env_vars = request.form.get('env_vars', '')
    try:
        env_file = os.path.join(get_repo_dir(project_name), '.env')
        with open(env_file, 'w') as f:
            f.write(env_vars)
        config = load_project_config(project_name)
        config['env_vars'] = env_vars
        save_project_config(project_name, config)
        return jsonify({'message': 'Environment variables saved'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def get_status():
    status = {"status": "stopped"}
    with process_lock:
        if current_process:
            if current_process.poll() is None:
                status = {
                    "status": "running",
                    "uptime": time.time() - process_info["start_time"],
                    "pid": process_info["pid"],
                    "project": process_info["project"]
                }
            else:
                status = {"status": "stopped"}
                current_process = None
    return jsonify(status)

@app.route('/output')
def get_output():
    project_name = request.args.get('project')
    with output_lock:
        if project_name:
            output = [item for item in output_buffer if item[2] == project_name]
        else:
            output = output_buffer.copy()
        output_buffer.clear()
    return jsonify(output)

@app.route('/projects')
def list_projects():
    projects = []
    if os.path.exists(REPOS_DIR):
        projects = [name for name in os.listdir(REPOS_DIR) 
                   if os.path.isdir(os.path.join(REPOS_DIR, name))]
    return jsonify(projects)

@app.route('/switch_project/<project_name>')
def switch_project(project_name):
    session['active_project'] = project_name
    return jsonify({'message': f'Switched to {project_name}'})

@app.route('/health')
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": time.time(),
        "version": "1.0"
    })

@app.route('/resources')
def resource_usage():
    return jsonify({
        "memory": psutil.virtual_memory()._asdict(),
        "cpu": psutil.cpu_percent(),
        "disk": psutil.disk_usage('/')._asdict()
    })

# --- Global error handler to return JSON for all errors ---
@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors
    if isinstance(e, HTTPException):
        response = e.get_response()
        # Replace the body with JSON
        response.data = json.dumps({
            "error": e.description,
            "code": e.code
        })
        response.content_type = "application/json"
        return response
    # Non-HTTP exceptions
    return jsonify({
        "error": str(e),
        "code": 500
    }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)