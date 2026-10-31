"""Platform adapters (specification §11).

Every platform-specific behavior — validation, formatting, API calls, and
error interpretation — lives inside its adapter package. Core modules
communicate with adapters only through the shared typed interface in
`publisher.core.adapters` and never contain platform conditionals.
"""
