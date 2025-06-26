# Repo Runner

Repo Runner is a web-based dashboard for managing, building, and running code repositories. It provides a simple interface to clone repositories, manage virtual environments, run build/start commands, and edit environment variables—all from your browser.

## Features
- **Repository Management**: Clone, delete, and update git repositories.
- **Virtual Environment**: Recreate Python virtual environments and install dependencies.
- **Build & Run**: Run custom build and start commands for your project.
- **Environment Variables**: Edit and save environment variables for your project.
- **Output Console**: View command output and errors in real time.

## Usage

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Set your SECRET_KEY**
   - Copy `.env.example` to `.env` and set a secure `SECRET_KEY`.
   - Or set the `SECRET_KEY` environment variable in your shell.
3. **Run the dashboard**
   ```bash
   python main.py
   ```
4. **Open your browser**
   - Go to `http://localhost:5000` (or the port you set in `.env`)

## Project Structure
- `main.py` — Flask app and backend logic
- `templates/index.html` — Main dashboard UI
- `static/style.css` — Custom styles
- `.env.example` — Example environment variables
- `requirements.txt` — Python dependencies

## Notes
- The dashboard creates a `repo/` directory for the cloned repository and a `venvs/` directory for virtual environments.
- All commands are run inside the context of the cloned repository and its virtual environment.

## Security
- Always set a strong `SECRET_KEY` in production.
- This tool is intended for local/development use. Do not expose it to the public internet without proper security measures.

## License
MIT License
