# Implementation & Verification Guide

**Duration:** ~30 minutes  
**Risk:** LOW (config-only change)  
**Rollback:** Simple (revert field_mapping in YAML)

---

## Phase 1: Pre-Deployment Verification (5 min)

### Step 1.1: Verify Changed File
```bash
cd global-banking-reconciliation-pipeline
git diff configs/validation_rules.yaml
```

**Expected output:**
```diff
-  transaction_id: txn_id
+  transaction_id: txn_id_standardized
-  transaction_date: txn_date_parsed
+  transaction_date: messy_txn_date_parsed
-  transaction_date_raw_fallback: txn_date
+  transaction_date_raw_fallback: messy_txn_date
-  currency_code_secondary: currency_code
+  currency_code_secondary: operating_currency
```

### Step 1.2: Verify YAML Syntax
```bash
python -c "import yaml; yaml.safe_load(open('configs/validation_rules.yaml'))"
```

**Expected:** No errors

### Step 1.3: Backup Current Configuration
```bash
cp configs/validation_rules.yaml configs/validation_rules.yaml.backup
```

---

## Phase 2: Test Deployment (10 min)

### Step 2.1: Run Validation on Test Batch
```bash
python main.py --pipeline-stage validation --test-mode true
```

Or if running full pipeline:
```bash
python main.py
```

### Step 2.2: Monitor Output
Check the validation logs:
```bash
tail -f logs/validation.log
```

**Expected messages:**
```
INFO: Discovered 51 silver CSV file(s)
INFO: Validated enterprise_banking_messy_dataset_xxx: valid=48000+ warning=1000-3000 rejected=1000-2000
```

### Step 2.3: Check Profiling Reports
```bash
python -c "
import pandas as pd
df = pd.read_csv('outputs/profiling_reports/validation_summary.csv')
print('VALIDATION SUMMARY:')
print(df[['source_file', 'rows_valid', 'rows_warning', 'rows_rejected']].head(10))
print()
print('TOTALS:')
print(f'Total Valid:    {df.rows_valid.sum():,} ({100*df.rows_valid.sum()/df.rows_processed.sum():.1f}%)')
print(f'Total Warning:  {df.rows_warning.sum():,} ({100*df.rows_warning.sum()/df.rows_processed.sum():.1f}%)')
print(f'Total Rejected: {df.rows_rejected.sum():,} ({100*df.rows_rejected.sum()/df.rows_processed.sum():.1f}%)')
"
```

**Expected:**
```
TOTALS:
Total Valid:    2,000,000+ (75-98%)
Total Warning:  100,000-300,000 (2-15%)
Total Rejected: 50,000-150,000 (2-10%)
```

---

## Phase 3: Detailed Validation (10 min)

### Step 3.1: Compare Before/After Distribution

**Create comparison script:**
```bash
cat > /tmp/validate_calibration.py << 'EOF'
import pandas as pd

# Expected changes
before = {
    'valid': 0,
    'warning': 15641 * 51,  # ~798,000 across all batches
    'rejected': 35362 * 51   # ~1,803,462
}

actual = pd.read_csv('outputs/profiling_reports/validation_summary.csv')
after = {
    'valid': actual['rows_valid'].sum(),
    'warning': actual['rows_warning'].sum(),
    'rejected': actual['rows_rejected'].sum()
}

total = sum(after.values())

print("VALIDATION CALIBRATION RESULTS")
print("=" * 60)
print(f"\nBefore Calibration:")
print(f"  VALID:    {before['valid']:,} (0.0%)")
print(f"  WARNING:  {before['warning']:,} ({100*before['warning']/total:.1f}%)")
print(f"  REJECTED: {before['rejected']:,} ({100*before['rejected']/total:.1f}%)")

print(f"\nAfter Calibration:")
print(f"  VALID:    {after['valid']:,} ({100*after['valid']/total:.1f}%)")
print(f"  WARNING:  {after['warning']:,} ({100*after['warning']/total:.1f}%)")
print(f"  REJECTED: {after['rejected']:,} ({100*after['rejected']/total:.1f}%)")

print(f"\nEnterprise Target:")
print(f"  VALID:    75-85%")
print(f"  WARNING:  10-15%")
print(f"  REJECTED: 5-10%")

# Pass/Fail
valid_ok = 75 <= 100*after['valid']/total <= 98
warn_ok = 2 <= 100*after['warning']/total <= 20
rej_ok = 2 <= 100*after['rejected']/total <= 10

status = "✓ PASS" if (valid_ok and warn_ok and rej_ok) else "❌ FAIL"
print(f"\nStatus: {status}")

if not valid_ok:
    print(f"  ⚠️ Valid records outside expected range")
if not warn_ok:
    print(f"  ⚠️ Warning records outside expected range")
if not rej_ok:
    print(f"  ⚠️ Rejected records outside expected range")
EOF

python /tmp/validate_calibration.py
```

### Step 3.2: Review Rejection Reasons

**Check what reasons remain for rejections:**
```bash
python -c "
import pandas as pd

# Get sample rejected records
gold = pd.read_csv('data/gold/*__*.csv', error_bad_lines=False)
rejects = pd.read_csv('data/rejects/*_rejected__*.csv', error_bad_lines=False)

if len(rejects) > 0:
    print('SAMPLE REJECTION RULES:')
    top_rules = rejects['validation_rule_triggered'].value_counts().head(10)
    for rule, count in top_rules.items():
        print(f'  {rule}: {count}')
    print()
    print('Expected rules: date_impossible, amount_overflow, identifier_*')
    print('                enum_invalid_*, duplicate_transaction_id')
    print()
    print('Unexpected rules (should be ~0): mandatory_*')
else:
    print('No rejections found - verify file paths')
"
```

### Step 3.3: Review Warning Reasons

**Check what warnings are in the warnings dataset:**
```bash
python -c "
import pandas as pd
import glob

warn_files = glob.glob('data/rejects/warnings/*_warnings__*.csv')
if warn_files:
    warnings = pd.concat([pd.read_csv(f, low_memory=False) for f in warn_files])
    print('SAMPLE WARNING RULES:')
    top_rules = warnings['validation_rule_triggered'].value_counts().head(10)
    for rule, count in top_rules.items():
        print(f'  {rule}: {count}')
else:
    print('No warnings found or check file paths')
"
```

**Expected top warnings:**
- `enum_invalid_*` rules
- `identifier_*_malformed` rules
- `date_parse_failed` (if any remain)
- `date_future`
- `duplicate_transaction_id`

---

## Phase 4: Audit Verification (5 min)

### Step 4.1: Check Audit Log

**Verify audit log contains correct field references:**
```bash
python -c "
import pandas as pd

audit = pd.read_csv('data/audit/validation_audit_log.csv')
print('Sample audit entries (first 10 violations):')
print(audit[['source_file', 'rule_name', 'severity', 'validation_status']].head(10))
print()

# Check that mandatory_transaction_id is rare (should be ~0)
mandatory_id_count = len(audit[audit['rule_name'] == 'mandatory_transaction_id'])
print(f'mandatory_transaction_id violations: {mandatory_id_count}')
print(f'Expected: <100 (essentially 0)')

# Check that mandatory_transaction_date is rare
mandatory_date_count = len(audit[audit['rule_name'] == 'mandatory_transaction_date'])
print(f'mandatory_transaction_date violations: {mandatory_date_count}')
print(f'Expected: <100 (essentially 0)')
"
```

### Step 4.2: Verify Gold Record Quality

**Sample gold records:**
```bash
python -c "
import pandas as pd
import glob

gold_files = glob.glob('data/gold/*__*.csv')
if gold_files:
    gold = pd.concat([pd.read_csv(f, low_memory=False) for f in gold_files[:3]])
    
    print('GOLD RECORD SAMPLE (first 5):')
    print(gold[['txn_id_standardized', 'messy_txn_date_parsed', 'messy_amount_amount_numeric', 
                'messy_amount_currency_code', 'validation_status']].head(5))
    print()
    
    # Check all required fields are present
    required_cols = ['txn_id_standardized', 'messy_txn_date_parsed', 
                     'messy_amount_amount_numeric', 'messy_amount_currency_code']
    for col in required_cols:
        if col in gold.columns:
            null_count = gold[col].isna().sum() + (gold[col].astype(str).str.strip() == '').sum()
            print(f'{col}: {null_count} nulls ({100*null_count/len(gold):.1f}%)')
    print()
    print('Expected: All should be 0-2% null (valid records only)')
"
```

---

## Phase 5: Deployment to Production (1 min)

### Option A: Direct Deployment (if tests pass)
```bash
# Changes are already in configs/validation_rules.yaml
# Just commit and deploy
git add configs/validation_rules.yaml
git commit -m "Validation calibration: Fix field mapping for S5 mandatory fields

- transaction_id: Use txn_id_standardized (0% null vs 63% raw)
- transaction_date: Use messy_txn_date_parsed (0% null vs 40% parsed)
- currency_code_secondary: Use operating_currency (better coverage)

Expected outcome: Valid gold records 75-98%, rejections 2-10%"

git push origin main
```

### Option B: Gradual Rollout (if higher caution needed)
```bash
# Run on subset of incoming files first
# Configure pipeline to process specific branches/countries
# Monitor validation distribution over 24 hours
# Expand to full volume when stable
```

---

## Phase 6: Rollback Plan (Emergency)

If results are unexpected:

```bash
# Restore backup configuration
cp configs/validation_rules.yaml.backup configs/validation_rules.yaml

# Verify restoration
git diff configs/validation_rules.yaml
# Should show no changes

# Re-run validation
python main.py --pipeline-stage validation

# The system will revert to previous behavior (~0% valid, ~69% rejected)
```

---

## Success Criteria Checklist

- [ ] YAML file syntax valid (no parsing errors)
- [ ] Validation completes without errors
- [ ] Valid records increase from 0% to 75%+
- [ ] Rejection rate decreases from 69% to <10%
- [ ] Warning records in 5-15% range
- [ ] Audit log shows <100 "mandatory_transaction_id" violations (vs 32K before)
- [ ] Audit log shows <100 "mandatory_transaction_date" violations (vs 20K before)
- [ ] Gold records have no null transaction_id or transaction_date
- [ ] Remaining rejections are for genuine issues (date_impossible, identifier_malformed, etc.)

---

## Troubleshooting

### Problem: Valid records still <50%

**Check 1:** Is YAML update applied?
```bash
grep "transaction_id: txn_id_standardized" configs/validation_rules.yaml
# Should return the line, not empty
```

**Check 2:** Is silver layer present and readable?
```bash
ls -la data/silver/ | head
# Should show CSV files
```

**Check 3:** Are the new columns present in silver?
```bash
python -c "
import pandas as pd
df = pd.read_csv('data/silver/enterprise_banking_messy_dataset_*.csv', nrows=1)
cols = ['txn_id_standardized', 'messy_txn_date_parsed', 'operating_currency']
for col in cols:
    print(f'{col}: {col in df.columns}')"
```

### Problem: validation_summary.csv shows 0 valid records

**Check:** Is validation running with updated config?
```bash
# Verify config is loaded
python -c "
import yaml
rules = yaml.safe_load(open('configs/validation_rules.yaml'))
print(rules['field_mapping']['transaction_id'])
# Should print: txn_id_standardized"
```

### Problem: Warnings spiking above 20%

This is likely fine - it means previously-false-rejected records are now marked as WARNING. Sample the warnings to verify:
```bash
# Check warning distribution
python -c "
import pandas as pd
warnings = pd.read_csv('data/rejects/warnings/*_warnings__*.csv')
print(warnings['validation_rule_triggered'].value_counts())
"
```

**Expected warnings:** Date format issues, enum violations, identifier patterns  
**Unexpected:** Mandatory field violations (would indicate data quality issue)

---

## Performance Expectations

- Validation stage: ~15-20 sec per 51K records (unchanged)
- Gold output: ~40MB per 51K valid records (up from near-zero)
- Warnings output: ~10-20MB per 51K records
- Rejects output: ~10-20MB per 51K records
- Audit log: ~50-100MB cumulative

---

## Monitoring (First 24 Hours Post-Deployment)

**Set up alert if:**
1. Valid records < 70% (indicates config not applied)
2. Rejection rate > 15% (indicates new data quality issue)
3. Mandatory field violations > 1,000 (indicates column mismatch)
4. Validation stage fails (indicates code issue)

**Expected norm:**
- Valid: 75-98% ↑ from 0%
- Warning: 2-15% ↑ from 30%
- Rejected: 2-10% ↓ from 69%

---

**Document Version:** 1.0  
**Last Updated:** May 15, 2025  
**Contact:** Data Pipeline Team
