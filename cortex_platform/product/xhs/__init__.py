"""First-party XHS plugin logic that needs no third-party code.

Provider HTTP lives in the research profile and runs only in engine children.
What sits here is pure: the identification prompt, the parsing of model
answers, the verbatim filter and merge rules, and title matching for link
verification. The engine child imports it to build requests and validate
answers; cortexd can re-run the same functions on its own copy of the inputs.
"""
