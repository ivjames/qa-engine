// PM2 process definition for qa-engine (lab980 convention).
// Fork mode, explicit — never set `instances` (that silently flips pm2 into
// cluster mode, and cluster-mode crashes land in ~/.pm2/pm2.log with empty app
// logs). gunicorn owns threading via gthread workers; pm2 just supervises the
// one fork process. SSE is long-lived, so --timeout 0 with app-level heartbeats.
//
// `qa-engine deploy` does the first `pm2 start ecosystem.config.cjs --only
// qa-engine` from a scrubbed environment (see bin/qa-engine); everything the
// app needs beyond PORT has to come from the app-dir .env, not from here and
// not from the shell. cwd is this file's directory so the checkout can live
// anywhere the CLI does.
module.exports = {
  apps: [
    {
      name: "qa-engine",
      cwd: __dirname,
      script: ".venv/bin/gunicorn",
      args: "--worker-class gthread --workers 1 --threads 4 --timeout 0 --graceful-timeout 30 --bind 127.0.0.1:8044 app:app",
      exec_mode: "fork",
      interpreter: "none",
      env: {
        PORT: "8044",
      },
      max_restarts: 10,
      merge_logs: true,
      error_file: "data/pm2-error.log",
      out_file: "data/pm2-out.log",
    },
  ],
};
