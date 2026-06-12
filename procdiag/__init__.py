"""procdiag — local-first Linux host resource diagnosis tool.

Records CPU, memory, and disk metrics over time using a layered sampler
architecture, then applies deterministic rules to explain why a machine is
slow, crashing, or running out of space.

This is the sibling tool of *netdiag* (network-check-tool): same design
vocabulary, same code idioms, same user-facing output style.
"""

__version__ = "0.2.0"
