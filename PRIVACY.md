# Privacy Policy

This plugin routes chat requests to OpenAI-compatible API endpoints that **you, the
workspace administrator, configure**.

- **Where your data goes:** the full prompt (message text, tool definitions/arguments,
  and image parts when vision is enabled) of each Dify request is forwarded over HTTPS
  to exactly one configured target `base_url` and its API key. Routing only chooses
  among endpoints you entered; the plugin does not send data anywhere else.
- **Storage:** none. The plugin keeps no persistent store and does not upload prompts,
  conversations, usage statistics, or telemetry to the plugin author.
- **Credentials:** API keys are entered in the plugin's secret field
  (`credentials_map`) and are used solely to build the `Authorization: Bearer` header
  for the matching upstream. Keys are never included in rule documents, model
  responses, or error messages.
- **Logs/errors:** failure messages contain the target id and upstream HTTP status
  only — never request bodies or keys.
- **Install-time traffic:** the Dify plugin daemon downloads pinned dependencies
  (`dify-plugin`, `requests`) from PyPI when the plugin is installed.
- Save-time validation sends one minimal `"ping"` message to every configured target.

Review your organization's policies before routing sensitive content to third-party
inference endpoints. Keyword routing is not a data-loss-prevention control.
