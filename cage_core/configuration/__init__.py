"""Cage configuration implementation, organized by responsibility.

Use :mod:`cage_core.config` for the compatibility API. Internal modules import
their dependency owners directly; the facade is never an implementation layer.

Schema and rendering validate values without performing runtime operations.
Selection, storage, and Codex adapters own their respective input boundaries.
Resolution composes selected capabilities; editing, interaction, OAuth, and UI
transactions build on those contracts. The CLI dispatches into these services.
"""
