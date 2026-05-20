# Compliance FAQ

## Overview
This document covers our compliance posture across HIPAA, GDPR, SOC 2 Type II, ISO 27001, and data residency. Reference this document when handling compliance inquiries, enterprise security questionnaires, or legal data requests.

---

## HIPAA Compliance

### Do You Support HIPAA?
Yes. We support HIPAA-compliant deployments for healthcare organizations handling Protected Health Information (PHI).

### Business Associate Agreement (BAA)
- BAA is available on **Enterprise plan only**
- BAA is not available on Starter, Standard, or Professional plans
- To request a BAA: contact compliance@company.com with subject "BAA Request"
- BAA review and signing typically takes 5-7 business days
- We use a standard BAA template; customer-provided BAA templates accepted with legal review

### HIPAA-Compliant Configuration Requirements
When using our platform for PHI, customers must:
1. Enable encryption at rest (available on Enterprise)
2. Enable audit logging for all data access (available on Enterprise)
3. Configure automatic session timeout (15 minutes recommended)
4. Restrict data export permissions to authorized users only
5. Use SSO with MFA for all users who access PHI

### What We Cover Under BAA
- Email content storage and processing
- Contact data and CRM records
- API data transmission
- Backup and disaster recovery

### What Is NOT Covered
- Third-party integrations not listed in our sub-processor list
- Data processed by customer's own webhooks or API integrations
- Any data stored outside our platform

### HIPAA Technical Safeguards
- Encryption in transit: TLS 1.2+
- Encryption at rest: AES-256
- Access controls: Role-based permissions, audit trails
- Automatic logoff: Configurable (required for HIPAA)
- Unique user identification: Enforced, shared accounts not permitted

---

## GDPR Compliance

### Are You GDPR Compliant?
Yes. We are fully GDPR compliant as both a data processor (when processing customer data on your behalf) and in our own operations as a data controller.

### Data Processing Agreement (DPA)
- DPA is available on all paid plans
- To request a DPA: email privacy@company.com
- Standard DPA template provided; typically signed within 3 business days
- Our DPA covers Standard Contractual Clauses (SCCs) for international transfers

### Data Subject Rights We Support

#### Article 15 — Right of Access
- Data subjects can request a copy of all personal data we hold
- We respond within 30 days (statutory requirement)
- Data provided in machine-readable format (JSON or CSV)

#### Article 16 — Right to Rectification
- Inaccurate personal data corrected within 30 days of verified request
- Contact privacy@company.com

#### Article 17 — Right to Erasure (Right to Be Forgotten)
- Data deleted within 30 days of verified request
- Exceptions: data required for legal obligations, active contracts
- Deletion includes backups within 90 days

#### Article 20 — Right to Data Portability
- **Statutory deadline: 30 days from receipt of valid request**
- Data provided in commonly used, machine-readable format (JSON)
- Includes: account data, email history, contact records, usage data
- To submit: email privacy@company.com with subject "GDPR Data Portability Request"
- We will acknowledge receipt within 72 hours
- Identity verification required before data release
- This is a **legal obligation** — failure to comply within 30 days is a regulatory violation

#### Article 21 — Right to Object
- Customers can object to processing for direct marketing
- Honored immediately upon receipt of valid objection

### GDPR Request Handling — Critical Notes
1. **Never treat a formal GDPR request as a generic inquiry**
2. GDPR requests must be flagged for the legal/compliance team immediately
3. The 30-day clock starts from receipt — delays are a regulatory risk
4. Auto-acknowledgement must be sent within 72 hours confirming receipt and timeline
5. Identity must be verified before releasing any data
6. Log all GDPR requests in the compliance tracker

### Data Retention
- Active customer data: retained for duration of contract + 30 days
- Deleted account data: purged within 30 days
- Backup data: purged within 90 days of deletion request
- Financial records: retained 7 years (legal requirement, exempt from erasure)
- Audit logs: retained 2 years

### Sub-Processors
Our current sub-processor list is published at: company.com/subprocessors
Key sub-processors include: AWS (infrastructure), Stripe (payments), SendGrid (transactional email)
Customers are notified 30 days before adding new sub-processors.

---

## SOC 2 Type II

### Current Status
- **SOC 2 Type II certified** — audit completed annually
- Current certification period: covers the 12-month period ending September 30, 2023
- Auditor: [Big 4 accounting firm]
- Trust Service Criteria covered: Security, Availability, Confidentiality

### Accessing Our SOC 2 Report
- SOC 2 report available under NDA to Enterprise customers and serious prospects
- To request: email security@company.com with subject "SOC 2 Report Request"
- NDA required before report is shared
- Report shared within 2 business days of signed NDA

### What SOC 2 Type II Means
- Type II (not just Type I) means our controls were tested over a 12-month period
- Independent auditor verified our security controls actually work in practice
- Re-audited annually — report is always less than 12 months old

---

## ISO 27001

### Current Status
- ISO 27001:2022 certification in progress
- Expected certification: Q2 2024
- Currently operating under ISO 27001 controls framework

### For RFP Responses
Until certification is complete:
- We can provide our Information Security Policy
- We can complete ISO 27001-based security questionnaires
- We can provide evidence of our security controls
- Enterprise customers can conduct their own security assessments

---

## Data Residency

### Available Regions
| Region | Plans Available | Notes |
|--------|----------------|-------|
| US (us-east-1) | All plans | Default region |
| EU (eu-west-1) | Enterprise only | GDPR preferred region |
| APAC (ap-southeast-1) | Enterprise only | Singapore |

### Data Residency Guarantees
- Enterprise customers can specify data residency region in their contract
- All data (primary, backup, logs) stored in selected region
- No data leaves the specified region except for sub-processors listed in DPA
- Data residency is a contractual commitment, not just a best-effort setting

---

## Security Practices

### Encryption
- In transit: TLS 1.2 minimum, TLS 1.3 preferred
- At rest: AES-256 for all stored data
- Database encryption: transparent data encryption enabled
- Backup encryption: same standard as primary data

### Access Controls
- All internal access requires MFA
- Production access limited to on-call engineers
- All production access logged and reviewed weekly
- Principle of least privilege enforced

### Vulnerability Management
- Penetration testing: annually by third-party firm
- Bug bounty program: security@company.com (HackerOne program coming Q1 2024)
- CVE monitoring: automated scanning, critical CVEs patched within 24 hours
- Security patches: critical (within 24h), high (within 7 days), medium (within 30 days)

### Incident Response
- Security incidents follow our Incident Response Plan (available to Enterprise customers)
- Data breach notification: within 72 hours of confirmed breach (GDPR requirement)
- Customers notified if their data is affected by a security incident

---

## Frequently Asked Questions

**Can we use your platform for financial services (PCI DSS)?**
We are not PCI DSS certified. Do not store full credit card numbers in our platform. Payment references and last-4-digits are acceptable.

**Do you support SSO?**
Yes, SSO via SAML 2.0 and OAuth 2.0 is available on Professional and Enterprise plans.

**What is your data breach notification process?**
We notify affected customers within 72 hours of confirming a breach, as required by GDPR. Notification includes: what data was affected, what we are doing about it, and what customers should do.

**Can we conduct a security audit or penetration test?**
Enterprise customers may conduct security assessments with prior written approval. Contact security@company.com at least 2 weeks in advance.

**Where is our data stored?**
By default, US region. Enterprise customers can specify EU or APAC. See Data Residency section above.