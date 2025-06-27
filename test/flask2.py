from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return 'Hello from flask2 on port 5002!'

if __name__ == '__main__':
    app.run(port=5002)
