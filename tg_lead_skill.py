"""Backwards-compatible entrypoint: the bot now lives in the hermes package."""

from hermes.app import run

if __name__ == "__main__":
    run()
