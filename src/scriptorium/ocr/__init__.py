"""Server-side OCR (Architecture §8): keys and modes, spend caps, the prompt, the API client.

``client.py`` is the only module in the package (and in Scriptorium) that imports the
``anthropic`` SDK. Requests carry a page image and the prompt only: never a document name, a
path, tags or a user identifier (§8.2). Keys exist as ``SecretStr`` and are never logged.
"""
