CYPRA MATRIX STUDIO OFFLINE DEPENDENCY KIT

Setup contains installers and Python packages only; the main MatrixStudio
folder remains the application.

On a fresh PC, keep Setup beside MatrixStudio and run:
  Setup\OFFLINE_SETUP.bat "C:\Path\To\MatrixStudio"

Then launch the main application with:
  MatrixStudio\START.bat

Included:
- Python 3.12.10 installer
- Ollama installer/executable supplied by the owner
- Offline Python package cache in python_packages\

The package cache must contain FastAPI, Uvicorn, OpenAI, Requests, HTTPX,
python-multipart, Pydantic, Pillow, pywebview, and pywinpty. Ollama model
weights are not duplicated here; copy the required Ollama model store to the
target PC or install/pull models before using chat.
