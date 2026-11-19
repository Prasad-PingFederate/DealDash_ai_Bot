module.exports = {
  apps: [
    {
      name: 'dealdash-bot',
      script: 'scripts/production_runner.py',
      interpreter: 'python3',
      args: '--continuous --poll-interval 30',
      autorestart: true,
      max_restarts: 50,
      restart_delay: 5000,
      kill_timeout: 10000,
      env: {
        CONTINUOUS: 'true',
        LOG_LEVEL: 'INFO',
        POLL_INTERVAL_MINUTES: '30'
      }
    }
  ]
};
