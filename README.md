# Attendance Analytics

This API records employee attendance in MongoDB. It supports employee setup, punch-in and punch-out, corrections, and attendance reports.

You need Python 3.11 or newer and a MongoDB connection. Set `MONGO_URI` and `MONGO_DB` in your environment or in a local `.env` file. Keep the URI private.

From this folder in PowerShell, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` to try the endpoints. The app creates its MongoDB indexes when it starts.
