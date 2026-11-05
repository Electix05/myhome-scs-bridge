#!/usr/bin/with-contenv bashio
# The bridge reads /data/options.json itself and asks the Supervisor for the
# MQTT service, so this wrapper only has to start it.  "exec" hands the process
# over: Python receives SIGTERM directly and can say "offline" before it ends.
bashio::log.info "Starting MyHome SCS Bridge..."
cd /opt/bridge || bashio::exit.nok "Cannot enter /opt/bridge"
exec python3 -m app
