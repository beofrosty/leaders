from app import create_app

try:
    from .app import create_app  # когда модуль импортируют как package: forum.run
except ImportError:
    from app import create_app   # когда запускаешь python run.py из папки forum
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
