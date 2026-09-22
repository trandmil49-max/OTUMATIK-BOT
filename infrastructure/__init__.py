"""infrastructure/ -- adapters to the outside world (database, exchange, Telegram, ...).

Nothing under core/ or system/ ever imports from here (SRS Part 20 Clean
Architecture: dependencies point inward, infrastructure depends on core,
core never depends on infrastructure).
"""
