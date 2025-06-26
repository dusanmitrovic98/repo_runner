import os
import subprocess
import shutil
import threading
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# Global state for running process
current_process = None
process_lock = threading.Lock()

# Project paths
REPO_DIR = os.path.join(os.getcwd(), 'repo')
VENVS_DIR = os.path.join(os.getcwd(), 'venvs')

def get_project_name():
    """Extract project name from repo URL"""
    repo_url = session.get('repo_url', '')
    if repo_url:
        return repo_url.split('/')[-1].replace('.git', '')
    return 'myproject'

def get_venv_path():
    """Get path to virtual environment"""
    project_name = get_project_name()
    return os.path.join(VENVS_DIR, project_name)

def run_command(cmd, cwd=None, env=None, background=False):
    """Run shell command with optional virtualenv activation"""
    venv_path = get_venv_path()
    
    # Use virtualenv's Python on Unix
    if os.name == 'posix':
        python_bin = os.path.join(venv_path, 'bin', 'python')
        cmd = f'{python_bin} -c "{cmd}"' if not cmd.startswith(python_bin) else cmd
    
    # Use virtualenv's Python on Windows
    elif os.name == 'nt':
        python_bin = os.path.join(venv_path, 'Scripts', 'python.exe')
        cmd = f'{python_bin} -c "{cmd}"' if not cmd.startswith(python_bin) else cmd
    
    env_vars = os.environ.copy()
    if env:
        env_vars.update(env)
    
    if background:
        global current_process
        with process_lock:
            if current_process:
                current_process.terminate()
            current_process = subprocess.Popen(
                cmd, 
                shell=True, 
                cwd=cwd or REPO_DIR,
                env=env_vars,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        return None
    else:
        result = subprocess.run(
            cmd, 
            shell=True, 
            cwd=cwd or REPO_DIR,
            env=env_vars,
            capture_output=True,
            text=True
        )
        return result

@app.before_request
def require_secret_key():
    """Ensure SECRET_KEY is set before accessing dashboard"""
    if request.endpoint != 'static' and not os.environ.get('SECRET_KEY'):
        return "SECRET_KEY environment variable is required", 500

@app.route('/')
def index():
    """Main dashboard view"""
    return render_template('index.html', 
        repo_url=session.get('repo_url', ''),
        build_cmd=session.get('build_cmd', ''),
        start_cmd=session.get('start_cmd', ''),
        env_vars=session.get('env_vars', '')
    )

@app.route('/clone', methods=['POST'])
def clone_repo():
    """Clone repository endpoint"""
    repo_url = request.form.get('repo_url')
    if not repo_url:
        return jsonify({'error': 'Repository URL required'}), 400
    
    try:
        # Clear existing repo
        if os.path.exists(REPO_DIR):
            shutil.rmtree(REPO_DIR)
        
        # Clone new repo
        result = run_command(f'git clone {repo_url} {REPO_DIR}', cwd=os.getcwd())
        if result.returncode != 0:
            return jsonify({
                'error': f'Clone failed: {result.stderr}'
            }), 500
        
        session['repo_url'] = repo_url
        return jsonify({'message': 'Repository cloned successfully'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete_repo', methods=['POST'])
def delete_repo():
    """Delete repository endpoint"""
    try:
        if os.path.exists(REPO_DIR):
            shutil.rmtree(REPO_DIR)
        session.pop('repo_url', None)
        return jsonify({'message': 'Repository deleted'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/recreate_venv', methods=['POST'])
def recreate_venv():
    """Recreate virtual environment"""
    try:
        venv_path = get_venv_path()
        
        # Remove existing venv
        if os.path.exists(venv_path):
            shutil.rmtree(venv_path)
        
        # Create new venv
        os.makedirs(VENVS_DIR, exist_ok=True)
        run_command(f'python -m venv {venv_path}', cwd=os.getcwd())
        
        # Install requirements
        req_file = os.path.join(REPO_DIR, 'requirements.txt')
        if os.path.exists(req_file):
            pip_cmd = f'pip install -r {req_file}'
            result = run_command(pip_cmd)
            if result.returncode != 0:
                return jsonify({
                    'error': f'Dependency install failed: {result.stderr}'
                }), 500
        
        return jsonify({'message': 'Virtual environment recreated'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/build', methods=['POST'])
def run_build():
    """Run build command"""
    build_cmd = request.form.get('build_cmd')
    if not build_cmd:
        return jsonify({'error': 'Build command required'}), 400
    
    try:
        session['build_cmd'] = build_cmd
        result = run_command(build_cmd)
        if result.returncode != 0:
            return jsonify({
                'error': f'Build failed: {result.stderr}'
            }), 500
        return jsonify({'message': 'Build completed', 'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/start', methods=['POST'])
def run_start():
    """Start application"""
    start_cmd = request.form.get('start_cmd')
    if not start_cmd:
        return jsonify({'error': 'Start command required'}), 400
    
    try:
        session['start_cmd'] = start_cmd
        
        # Load environment variables
        env = {}
        env_file = os.path.join(REPO_DIR, '.env')
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.strip() and not line.startswith('#'):
                        key, value = line.strip().split('=', 1)
                        env[key] = value
        
        run_command(start_cmd, background=True, env=env)
        return jsonify({'message': 'Application started'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stop', methods=['POST'])
def stop_app():
    """Stop running application"""
    global current_process
    with process_lock:
        if current_process:
            current_process.terminate()
            current_process = None
    return jsonify({'message': 'Application stopped'})

@app.route('/pull', methods=['POST'])
def pull_updates():
    """Pull latest changes"""
    try:
        result = run_command('git pull')
        if result.returncode != 0:
            return jsonify({
                'error': f'Pull failed: {result.stderr}'
            }), 500
        return jsonify({'message': 'Updates pulled', 'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/save_env', methods=['POST'])
def save_env():
    """Save environment variables"""
    env_vars = request.form.get('env_vars', '')
    try:
        env_file = os.path.join(REPO_DIR, '.env')
        with open(env_file, 'w') as f:
            f.write(env_vars)
        session['env_vars'] = env_vars
        return jsonify({'message': 'Environment variables saved'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)