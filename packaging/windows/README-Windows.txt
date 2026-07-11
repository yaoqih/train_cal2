Train Calculation Four-Stage API - Windows x64
================================================

Requirements
------------
- Windows Server 2019 or later, or Windows 10/11 x64.
- No separate Python installation is required.

Start
-----
1. Extract the complete ZIP. Do not run the EXE from inside the ZIP.
2. Copy server.env.example.cmd to server.env.cmd.
3. Replace TRAIN_CAL_API_KEY with a long private random value.
4. Run start-server.cmd.

The default address is http://0.0.0.0:8000. Useful checks:
  GET /healthz
  GET /readyz
  GET /api/plan/openapi.json

Configuration
-------------
The server reads the same TRAIN_CAL_* environment variables documented by the
project. start-server.cmd defaults to one solver worker to limit memory use.
Increase TRAIN_CAL_API_WORKERS only after measuring available memory.

Security
--------
Keep the API behind an HTTPS reverse proxy and firewall. Never publish
server.env.cmd or its API key. Job inputs, traces, results, and logs are stored
under artifacts\api_jobs unless TRAIN_CAL_API_JOB_ROOT is set.

The ZIP is a portable onedir build. Keep train-cal-server.exe and the
_internal directory together.
