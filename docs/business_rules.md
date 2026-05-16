# Business rules

Business rules for banking reconciliation will be **data-driven** and version-controlled rather than hard-coded in Python modules.

## Planned sources of truth

- **`configs/validation_rules.yaml`**: field-level and cross-field constraints, tolerances, and conditional rules.
- **`configs/currency_rules.yaml`**: supported currencies, rounding, and FX handling policies.
- **`configs/schema_mapping.yaml`**: per-feed column mapping into the canonical transaction model.
- **Reference tables** (CSV today; database or catalog later): geography, product, and counterparty mappings.

## Governance

When rules change, pipelines should record the **rule catalog version** in audit metadata so historical runs remain interpretable.

## Status

No business rules are active in this skeleton. Populate the YAML catalogs when requirements are finalized.
