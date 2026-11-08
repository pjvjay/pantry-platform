# Render hands a service's internal address as host:port (fromService ... property: hostport);
# the apps read URLs. Sourced by the API and hub images before they start.
if [ -n "${OLLAMA_HOSTPORT:-}" ] && [ -z "${OLLAMA_URL:-}" ]; then
  export OLLAMA_URL="http://${OLLAMA_HOSTPORT}"
fi
if [ -n "${PANTRY_API_HOSTPORT:-}" ] && [ -z "${PANTRY_API_URL:-}" ]; then
  export PANTRY_API_URL="http://${PANTRY_API_HOSTPORT}"
fi
