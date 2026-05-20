# Escalation Matrix

## Overview
This document defines who handles what, escalation chains, response time commitments, and the exact steps to take for each escalation type. Every agent decision to escalate must reference this document to produce a pre-filled brief for the receiving team.

---

## Escalation Types and Owners

### 1. Legal Threats & Cease and Desist
**Owner:** Head of Legal (legal@company.com)  
**Backup:** CEO  
**Response SLA:** Acknowledge within 2 hours, substantive response within 24 hours  

**What triggers this:**
- Any email containing: "cease and desist", "legal action", "lawsuit", "litigation", "attorney", "lawyer", "damages", "court"
- Formal legal notices from law firms
- Trademark or IP infringement claims
- Contract dispute escalations

**Steps:**
1. DO NOT auto-reply or send any response without legal approval
2. Flag email immediately using `flag_for_legal(email_id, issue_type)`
3. Forward full thread to legal@company.com with subject: "LEGAL ESCALATION — [Sender] — [Issue Type]"
4. Create internal ticket assigned to Legal team
5. Send holding reply ONLY if approved: "Thank you for your message. Our legal team is reviewing this matter and will respond within [timeframe]."
6. Log in audit trail with full context

**Pre-filled brief must include:**
- Sender name, company, email
- Full thread history
- Specific legal claim made
- Any monetary amounts mentioned
- Deadline given by sender (if any)
- Relevant contract or account details

---

### 2. Security Incidents

**Owner:** Head of Security (security@company.com)  
**Backup:** CTO  
**Response SLA:** Immediate (within 15 minutes for P0 security events)  

**What triggers this:**
- Ransomware or extortion threats
- Reported data breaches or unauthorized access
- Suspicious login alerts from unknown locations
- Reports of data exfiltration
- Social engineering attempts

**Steps — Ransomware / Extortion:**
1. **NEVER auto-reply to the attacker under any circumstances**
2. Immediately route to security queue
3. Alert security@company.com AND cto@company.com
4. Do not engage, negotiate, or acknowledge to the sender
5. Preserve all email headers and metadata for forensic analysis
6. Create P0 security incident ticket
7. Notify legal team if data breach is confirmed or suspected

**Steps — Suspicious Login Alert:**
1. Flag for security team immediately
2. Send security notification to account holder (not the suspicious IP)
3. Temporarily lock account pending verification
4. Security team investigates within 15 minutes

**Pre-filled brief must include:**
- Nature of threat (ransomware, breach, suspicious access, etc.)
- Sender information (if external threat actor)
- Any specific data or systems mentioned
- Timestamps
- Whether any internal systems may be compromised

---

### 3. GDPR and Legal Data Requests

**Owner:** Privacy/Compliance Officer (privacy@company.com)  
**Backup:** Head of Legal  
**Response SLA:** Acknowledge within 72 hours; fulfill within 30 days (legal requirement)  

**What triggers this:**
- Any mention of GDPR, CCPA, or other data protection regulations
- Formal data subject access requests
- Right to erasure requests
- Data portability requests (Article 20)
- Data processing objections

**Steps:**
1. Identify as legal compliance request — DO NOT treat as generic inquiry
2. Flag using `flag_for_legal(email_id, "GDPR")`
3. Send auto-acknowledgement immediately citing 30-day statutory window
4. Create compliance ticket assigned to Privacy Officer
5. Begin identity verification process
6. Log receipt timestamp — 30-day clock starts NOW
7. Do not share any data until identity is verified

**Auto-acknowledgement template:**
> "We have received your data request submitted under [GDPR Article X]. We are legally required to respond within 30 days of receiving a valid request. We will contact you within 72 hours to verify your identity before processing your request. Your request was received on [DATE] and our response deadline is [DATE + 30 days]."

---

### 4. P0 / Critical Outages

**Owner:** On-call Engineer (via PagerDuty)  
**Backup:** CTO  
**Response SLA:** 15 minutes initial response  

**What triggers this:**
- Customer reports complete service unavailability
- Multiple customers reporting the same issue simultaneously
- Monitoring alerts for P0 conditions
- Reports of data loss or corruption

**Steps:**
1. Check status page — is the incident already being tracked?
2. If not already tracked: create P0 incident ticket immediately
3. Page on-call engineer via PagerDuty
4. Send acknowledgement to customer within 15 minutes
5. Update customer every 30 minutes until resolved
6. After resolution: initiate RCA process (24-hour delivery SLA)

**Customer acknowledgement template:**
> "We've received your report and our engineering team is investigating with the highest priority. We will update you within 30 minutes. You can track real-time status at status.company.com."

---

### 5. VIP Customer Churn Risk

**Owner:** Head of Customer Success (cs@company.com)  
**Backup:** VP Sales  
**Response SLA:** Personal outreach within 2 hours  

**What triggers this:**
- VIP customer expresses dissatisfaction
- 3+ negative sentiment emails from high-value account
- Cancellation request from account >$500/month
- Review threat from any customer
- Unanswered complaint emails (3+ with no reply)

**Steps:**
1. Pull full account profile: tenure, spend, open issues, previous credits
2. Personal response from CS Manager (not support agent)
3. Offer appropriate retention credit per refund policy
4. Schedule call within 24 hours if possible
5. Escalate to Head of CS if account value >$1,000/month
6. Trigger web intelligence check for public review posts
7. If public review already posted: loop in marketing team

---

### 6. PR / Press Inquiries

**Owner:** Head of Marketing / PR (pr@company.com)  
**Backup:** CEO  
**Response SLA:** Acknowledge within 4 hours, substantive response within 24 hours  

**What triggers this:**
- Email from known press/media organization
- Request for comment or quote
- Inquiry about company news, funding, product
- Investor inquiries

**Steps:**
1. Do NOT provide any information, quotes, or comments without PR approval
2. Send holding reply: "Thank you for reaching out. I'm connecting you with the right person from our team who will follow up shortly."
3. Forward to pr@company.com immediately
4. Log in CRM with press inquiry tag

---

### 7. Enterprise Opportunity (High-Value Sales)

**Owner:** Head of Sales / Account Executive  
**Response SLA:** Same business day  

**What triggers this:**
- RFP or formal procurement inquiry
- Company with >500 employees expressing interest
- Estimated deal value >$10,000/year
- Multi-year contract discussion

**Steps:**
1. Do not let support handle enterprise sales inquiries
2. Immediately route to sales@company.com
3. Research company (size, industry, likely use case) before handing off
4. Create opportunity in CRM with deal size estimate
5. Assign to appropriate Account Executive based on geography/vertical

---

## Escalation Brief Template

Every escalation must include a structured brief so the receiving team has full context:

```
ESCALATION BRIEF
================
Type: [Legal / Security / GDPR / P0 / VIP Churn / PR / Enterprise]
Priority: [Critical / High / Medium]
Sender: [Name] <[email]> — [Company]
Thread: [thread_id] — [Subject]
Date of First Email: [date]
Date of Escalation: [today]

SITUATION SUMMARY:
[2-3 sentence summary of what the customer is saying and what's at stake]

THREAD HISTORY:
[Brief summary of each email in the thread]

KEY DETAILS:
- Account Value: $[amount]/month
- VIP Status: [Yes/No]
- Previous Credits Issued: [amount or none]
- Specific Deadline Given: [date or none]
- Legal/Regulatory Reference: [GDPR Article X, SLA clause, etc.]

WHAT THE AGENT HAS DONE:
[List of actions already taken: auto-acknowledgement sent, ticket created, etc.]

RECOMMENDED NEXT ACTION:
[Specific recommendation for the receiving team]

RELEVANT POLICY REFS:
[Which knowledge base documents are relevant]
```

---

## Escalation Anti-Patterns (What NOT to Do)

- **Never** auto-reply to ransomware, legal threats, or security incidents
- **Never** make promises about refund amounts or legal outcomes without approval
- **Never** admit liability in writing
- **Never** share another customer's data or information
- **Never** ignore a GDPR request or treat it as a generic inquiry
- **Never** let 3+ unanswered emails from the same sender go unescalated
- **Never** escalate without a brief — the receiving team needs context, not just a forwarded email

---

## Contact Directory

| Role | Email | Phone | Escalation Type |
|------|-------|-------|----------------|
| Head of Legal | legal@company.com | +1-555-0101 | Legal, IP, Contracts |
| Privacy Officer | privacy@company.com | +1-555-0102 | GDPR, Data Requests |
| Head of Security | security@company.com | +1-555-0103 | Security Incidents |
| Head of Customer Success | cs@company.com | +1-555-0104 | VIP Churn, Complaints |
| Head of Sales | sales@company.com | +1-555-0105 | Enterprise Opportunities |
| PR / Communications | pr@company.com | +1-555-0106 | Press, Investor Inquiries |
| CTO | cto@company.com | +1-555-0107 | P0 Technical Escalations |
| CEO | ceo@company.com | +1-555-0108 | Critical, All-Hands Escalations |