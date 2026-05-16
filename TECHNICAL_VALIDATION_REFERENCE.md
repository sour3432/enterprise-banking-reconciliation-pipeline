# Technical Reference: Validation Rules & Corrected Field Mapping
**Purpose:** Document which validation rules are affected by calibration changes

## Affected Validation Rules

### 1. Mandatory Field Rules (S5 FATAL)

#### Rule: `mandatory_transaction_id`
```yaml
Rule Logic: IF transaction_id IS EMPTY THEN S5
```

| Aspect | Before Calibration | After Calibration |
|--------|---|---|
| Checks column | `txn_id` | `txn_id_standardized` |
| Current nullness | 63.2% | 0% |
| Records rejected | 32,246/51,003 | 0/51,003 |
| Status | ❌ BROKEN | ✓ FIXED |

**Impact:** 32,246 false rejections eliminated

---

#### Rule: `mandatory_transaction_date`
```yaml
Rule Logic: IF (transaction_date IS EMPTY AND transaction_date_raw_fallback IS EMPTY) THEN S5
```

| Aspect | Before | After |
|--------|--------|-------|
| Primary checks | `txn_date_parsed` | `messy_txn_date_parsed` |
| Primary nullness | 40.6% | 0% |
| Fallback checks | `txn_date` | `messy_txn_date` |
| Fallback nullness | 40.6% | 0% |
| Combined coverage | 59.4% | 100% |
| Records rejected | 20,706/51,003 | 0/51,003 |
| Status | ❌ BROKEN | ✓ FIXED |

**Impact:** 20,706 false rejections eliminated

---

#### Rule: `mandatory_currency_code`
```yaml
Rule Logic: IF (currency_code_primary IS EMPTY AND currency_code_secondary IS EMPTY) THEN S5
```

| Aspect | Before | After |
|--------|--------|-------|
| Primary checks | `messy_amount_currency_code` | `messy_amount_currency_code` |
| Primary nullness | 16.8% | 16.8% |
| Fallback checks | `currency_code` | `operating_currency` |
| Fallback nullness | 77.4% | TBD (expected ~20-30%) |
| Combined coverage | 86.9% | ~85-90% |
| Records rejected | 6,700/51,003 | ~5,100-7,650/51,003 |
| Status | ⚠️ IMPROVED | ✓ BETTER |

**Impact:** ~500-1,600 additional records recovered

---

#### Rule: `mandatory_transaction_amount`
```yaml
Rule Logic: IF transaction_amount IS NULL AND transaction_amount_fallback IS NULL THEN S5
```

| Aspect | Before | After |
|--------|--------|-------|
| Primary checks | `messy_amount_amount_numeric` | `messy_amount_amount_numeric` |
| Primary nullness | 0% | 0% |
| Status | ✓ WORKING | ✓ NO CHANGE |

**Impact:** No change (already working)

---

#### Rule: `mandatory_account_id`
```yaml
Rule Logic: IF account_id IS EMPTY AND account_id_fallback IS EMPTY THEN S5
```

| Aspect | Before | After |
|--------|--------|-------|
| Primary checks | `primary_account_number_standardized` | `primary_account_number_standardized` |
| Primary nullness | 0% | 0% |
| Status | ✓ WORKING | ✓ NO CHANGE |

**Impact:** No change (already working)

---

## Unaffected Validation Rules

The following rules **do not reference** the corrected field mappings and require **no changes**:

### Date Range Validation (S3-S5)
- `date_future` → Validates against `transaction_date` (now `messy_txn_date_parsed`)
- `date_impossible` → Validates year range
- `date_parse_failed` → Checks parse failure flags

**Status:** ✓ Auto-fixed by primary date field correction

---

### Identifier Pattern Validation (S3)
- `identifier_ifsc_malformed` → Uses `destination_ifsc_standardized` ✓
- `identifier_bic_malformed` → Uses `swift_code_standardized` ✓
- `identifier_branch_short` → Uses `branch_code_standardized` ✓

**Status:** ✓ Already using correct standardized columns

---

### Enum Validation (S3)
- `enum_invalid_transaction_status` → Checks `status` column
- `enum_invalid_reconciliation_status` → Checks `reconciliation_status` column
- `enum_invalid_payment_channel` → Checks `channel` column
- `enum_invalid_debit_credit` → Checks `debit_credit` column

**Status:** ✓ Independent of field mapping changes

---

### FX Validation (S2-S3)
- `fx_unsupported_currency` → Validates currency codes
- `fx_invalid_rate` → Validates exchange rate
- `fx_missing_rate` → Checks when currency pair exists

**Status:** ✓ Independent of field mapping changes

---

### Amount Validation (S3-S4)
- `amount_non_positive` → Checks numeric value
- `amount_overflow` → Checks magnitude
- `amount_parse_failed` → Checks parse flags

**Status:** ✓ Uses correct `messy_amount_amount_numeric` column

---

### Data Quality Validation (S1-S2)
- `duplicate_transaction_id` → Checks for duplicate IDs
- `null_density_high` → Checks row-level nullness
- `placeholder_value` → Checks for placeholder tokens
- `unicode_replacement` → Checks for replacement characters

**Status:** ✓ No field mapping dependency

---

## Severity Configuration (No Changes)

```yaml
# Rejection threshold
reject_from_severity: S4  # Keep as-is (unchanged)

# Warning threshold  
warning_up_to_severity: S3  # Keep as-is (unchanged)

# Severity matrix
Mandatory fields:           S5 (FATAL)
Date impossible:            S5 (FATAL)
Date parse with fallback:   S4 (CRITICAL) 
Amount overflow:            S4 (CRITICAL)
Amount parse:               S4 (CRITICAL)
Identifiers malformed:      S3 (HIGH)
Date future:                S3 (HIGH)
Amount non-positive:        S3 (HIGH)
Enum violations:            S3 (HIGH)
FX violations:              S2-S3
Duplicate check:            S2 (MEDIUM)
Placeholder values:         S2 (MEDIUM)
Unicode issues:             S2 (MEDIUM)
Null density:               S1 (LOW)
```

**Note:** These severity levels are appropriate. The primary issue was field mapping, not severity miscalibration.

---

## Validation Flow After Calibration

```
SILVER LAYER (input)
    ├─ txn_id_standardized (0% null)
    ├─ messy_txn_date_parsed (0% null)
    ├─ messy_txn_date (0% null)
    ├─ messy_amount_amount_numeric (0% null)
    ├─ messy_amount_currency_code (16.8% null)
    ├─ operating_currency (expected ~20-30% null)
    └─ primary_account_number_standardized (0% null)
           ↓
    VALIDATION ENGINE
           ├─ Mandatory field checks (S5)
           │   ├─ transaction_id? ✓ (uses txn_id_standardized)
           │   ├─ transaction_date? ✓ (uses messy_txn_date_parsed)
           │   ├─ amount? ✓ (uses messy_amount_amount_numeric)
           │   ├─ currency_code? ✓ (uses messy_amount_currency_code + operating_currency)
           │   └─ account_id? ✓ (uses primary_account_number_standardized)
           ├─ Pattern/format checks (S3)
           │   ├─ IFSC pattern
           │   ├─ BIC pattern
           │   └─ Branch length
           ├─ Range checks (S3-S5)
           │   ├─ Date impossible year
           │   ├─ Date future
           │   └─ Amount overflow
           └─ Data quality checks (S1-S2)
               ├─ Duplicates
               ├─ Null density
               └─ Placeholder values
           ↓
    CLASSIFICATION
    ├─ VALID (no issues, 0% severity)
    ├─ WARNING (S1-S3 issues only)
    └─ REJECTED (S4-S5 issues)
           ↓
    OUTPUT
    ├─ data/gold/
    ├─ data/rejects/
    └─ data/rejects/warnings/
```

---

## Column Reference by Layer

### BRONZE (Raw Input)
```
txn_id (raw)
txn_date (raw)
amount (raw)
currency_code (raw)
account_number (raw)
... [257 columns total]
```

### SILVER (Standardized Output)
```
✓ txn_id_standardized       (standardized identifier)
✓ txn_date_parsed           (primary date parse)
✓ messy_txn_date_parsed     (alternative date parse) ← NOW USED
✓ messy_txn_date            (alternative raw date) ← NOW USED
✓ messy_amount_amount_numeric (standardized amount)
✓ messy_amount_currency_code (standardized currency)
✓ operating_currency        (schema-mapped currency) ← NOW USED
✓ primary_account_number_standardized (standardized account)
✓ [+300 other columns]

Confidence scores:
- txn_date_parse_confidence
- txn_date_parse_failed
- [similar for all parsed fields]
```

### GOLD (Validated Output)
```
[All SILVER columns + validation metadata]
- validation_timestamp
- validation_severity
- validation_status (VALID/WARNING/REJECTED)
- validation_rule_triggered
```

---

## Test Cases for Validation

### Test 1: Transaction ID Coverage
```
Input:  51,003 records with various txn_id values
Before: 32,246 REJECTED (txn_id missing in raw column)
After:  All PASSED (txn_id_standardized has 100% coverage)
```

### Test 2: Transaction Date Coverage
```
Input:  51,003 records with date values
Before: 20,706 REJECTED (txn_date_parsed missing or invalid)
After:  All PASSED (messy_txn_date_parsed has 100% coverage)
```

### Test 3: Currency Code Coverage
```
Input:  51,003 records with currency values
Before: 6,700 REJECTED (both primary and currency_code empty)
After:  ~5,100-6,000 PASSED (operating_currency fallback improves)
Remaining: ~700 (genuinely missing currency)
```

### Test 4: Audit Trail
```
Sample S5 rejection before:
  rule_name: mandatory_transaction_id
  source_column: txn_id (empty)
  
Sample S5 rejection after (should be rare):
  rule_name: mandatory_transaction_id
  source_column: txn_id_standardized (empty)
  Note: Would indicate data loss in standardization layer
```

---

**Document Version:** 1.0  
**Last Updated:** May 15, 2025  
**Status:** Ready for Implementation
