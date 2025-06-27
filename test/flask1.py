from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return 'Hello from flask1 on port 5003!'

if __name__ == '__main__':
    app.run(port=5003)
