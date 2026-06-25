"""
Backward-compatible domain loader module.

Some modules import `domains.loader`; the implementation currently lives in
`domains.__init__`. Re-export here so both import styles work.
"""

from domains import (  # noqa: F401
    DomainConfig,
    IssueTypeSpec,
    SpecialistSpec,
    get_all_domain_ids,
    get_domain_config,
    load_domain_config,
)

__all__ = [
    "load_domain_config",
    "get_domain_config",
    "get_all_domain_ids",
    "DomainConfig",
    "IssueTypeSpec",
    "SpecialistSpec",
]
