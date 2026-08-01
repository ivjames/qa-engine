// PM2 process definition for qa-engine (lab980 convention).
// Fork mode, explicit — never set `instances` (that silently flips pm2 into
// cluster mode, and cluster-mode crashes land in ~/.pm2/pm2.log with empty app
// logs). gunicorn owns threading via gthread workers; pm2 just supervises the
// one fork process. SSE is long-lived, so --timeout 0 with app-level heartbeats.
module.exports = {
  apps: [
    {
      name: "qa-engine",
      cwd: "/var/www/qa-engine",
      script: ".venv/bin/gunicorn",
      args: "--worker-class gthread --workers 1 --threads 4 --timeout 0 --graceful-timeout 30 --bind 127.0.0.1:8044 app:app",
      exec_mode: "fork",
      interpreter: "none",
      env: {
        PORT: "8044",
      },
      max_restarts: 10,
      error_file: "data/pm2-error.log",
      out_file: "data/pm2-out.log",
    },
  ],
};
