"""
engines/ -- business-logic orchestration layer (Module 5+).

Each module here corresponds to one "Engine" as named throughout the SRS
(Symbol Discovery, Fast Filter, Bitcoin Intelligence, Market Health,
Risk, Confidence, Coin Trust, ...). Engines depend on `core/`,
`infrastructure/`, `system/`, and `config/`; nothing in those layers ever
imports from `engines/` (SRS Part 20 Clean Architecture: dependencies
point inward, engines sit at the outermost/application layer).
"""
