# Service Level Agreement (SLA) Policy

## Overview
This document defines our uptime commitments, incident response times, credit calculation formulas, and root cause analysis (RCA) delivery obligations. These terms apply to all paid plans. Free trial accounts are excluded from SLA guarantees.

---

## Uptime Commitment

### By Plan
| Plan | Monthly Uptime Guarantee | Max Allowed Downtime/Month |
|------|--------------------------|---------------------------|
| Starter | 99.5% | 3 hours 39 minutes |
| Standard | 99.9% | 43 minutes 48 seconds |
| Professional | 99.9% | 43 minutes 48 seconds |
| Enterprise | 99.95% (negotiable to 99.99%) | 21 minutes 54 seconds |

### Uptime Calculation
- Uptime % = ((Total minutes in month - Downtime minutes) / Total minutes in month) × 100
- Total minutes in a 30-day month: 43,200
- Total minutes in a 31-day month: 44,640
- Scheduled maintenance windows do NOT count as downtime (see below)

---

## Scheduled Maintenance

- Scheduled maintenance is announced at least 72 hours in advance via email and status page
- Standard maintenance window: Saturdays 2:00 AM – 6:00 AM EST
- Emergency maintenance may occur with shorter notice; does not count toward SLA if announced before start
- Maximum scheduled downtime per month: 4 hours

---

## Incident Classification

### P0 — Critical (Production Down)
- **Definition:** Complete service unavailability affecting all users, or data loss/corruption
- **Examples:** Full outage, database failure, complete API unavailability
- **Initial Response SLA:** 15 minutes from detection
- **Status Update Frequency:** Every 30 minutes until resolved
- **Resolution Target:** 2 hours
- **RCA Delivery:** Within 24 hours of incident resolution

### P1 — High (Major Degradation)
- **Definition:** Significant performance degradation or partial outage affecting >50% of users
- **Examples:** Slow API response >5s, login failures for subset of users
- **Initial Response SLA:** 1 hour
- **Status Update Frequency:** Every 2 hours
- **Resolution Target:** 8 hours
- **RCA Delivery:** Within 48 hours of resolution

### P2 — Medium (Minor Degradation)
- **Definition:** Non-critical feature unavailable, affecting <50% of users
- **Initial Response SLA:** 4 hours (business hours)
- **Resolution Target:** 3 business days
- **RCA Delivery:** Not required; post-mortem optional

### P3 — Low (Cosmetic / Non-impacting)
- **Definition:** UI glitches, minor bugs, non-functional features with workarounds
- **Initial Response SLA:** 1 business day
- **Resolution Target:** Next release cycle

---

## SLA Credit Calculation

When uptime falls below the guaranteed threshold, affected customers are entitled to service credits.

### Credit Formula
```
Credit = (Downtime Minutes / Total Minutes in Month) × Monthly Fee
```

### Credit Tiers
| Uptime Achieved | Credit Percentage |
|-----------------|-------------------|
| 99.0% – 99.9% | 10% of monthly fee |
| 95.0% – 99.0% | 25% of monthly fee |
| 90.0% – 95.0% | 50% of monthly fee |
| Below 90.0% | 100% of monthly fee |

### Example Calculation — Bob's Scenario
- Bob's plan: Enterprise ($2,000/month)
- Incident: 47 minutes of downtime on October 1st
- Monthly uptime guarantee: 99.95% (max 21 min 54 sec allowed)
- Downtime exceeded SLA by: 47 - 21.9 = 25.1 minutes
- Uptime achieved: (43,200 - 47) / 43,200 = 99.89%
- Credit tier: 99.0%–99.9% = 10% of monthly fee
- **Credit owed: $200.00**

### Additional P0 RCA Credit
- If RCA is not delivered within 24 hours of P0 resolution: additional 5% credit
- If RCA is deemed inadequate (does not address root cause): customer may request review
- Inadequate RCA dispute process: submit to support within 7 days of RCA delivery

### Credit Limitations
- Credits are applied to future invoices only — not refunded as cash
- Maximum credit per month: 100% of monthly fee
- Credits do not carry over beyond 3 billing cycles
- Credits must be requested within 30 days of the incident

---

## How to Request SLA Credits

1. Email billing@company.com with subject: "SLA Credit Request — [Date of Incident]"
2. Include: account email, incident date, observed downtime duration
3. Our team verifies against internal monitoring data within 5 business days
4. Credit applied to next invoice if claim is validated

---

## Root Cause Analysis (RCA) Requirements

### P0 RCA Must Include
1. **Timeline:** Precise start time, detection time, mitigation time, resolution time
2. **Root Cause:** Technical explanation of what failed and why
3. **Impact Assessment:** Number of users affected, data impact, revenue impact estimate
4. **Immediate Remediation:** What was done to restore service
5. **Preventive Measures:** Specific engineering changes to prevent recurrence, with owners and deadlines
6. **Monitoring Improvements:** What new alerts or monitoring were added

### RCA Delivery Timeline
- P0: Within 24 hours of resolution — **this is a contractual obligation**
- P1: Within 48 hours of resolution
- Failure to deliver P0 RCA within 24 hours triggers automatic 5% additional credit

### What Constitutes an Inadequate RCA
- Missing root cause (says "unknown" without investigation evidence)
- No preventive measures listed
- No timeline provided
- Copy-paste of previous RCA without incident-specific details
- Customers may dispute inadequate RCAs within 7 days

---

## Exclusions from SLA

The following do not count as downtime for SLA purposes:
- Scheduled maintenance windows (announced 72h in advance)
- Issues caused by customer's own code, API misuse, or exceeding rate limits
- Force majeure events (natural disasters, large-scale internet outages)
- DDoS attacks (though we commit to best-effort mitigation)
- Issues caused by third-party services outside our control

---

## Enterprise SLA Addendum

Enterprise customers may negotiate custom SLA terms including:
- Higher uptime guarantees (up to 99.99%)
- Faster response times
- Cash refunds instead of credits (requires contract addendum)
- Dedicated incident response team
- Real-time incident bridge with customer's technical team

Contact your account manager to add custom SLA terms to your Enterprise contract.

---

## Status Page and Incident Communication

- Real-time status: status.company.com
- Subscribe to incident notifications via email or SMS on the status page
- All incidents are logged with timestamps and updates
- Post-incident RCAs are published on the status page within the delivery window