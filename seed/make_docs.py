"""Generate fictional aged-care admin documents (PDF) plus a manifest.json.

Everything here is invented for a demo: the facility, people, residents,
suppliers and figures. Every page carries a "fictional demo document" footer.

Output is byte-for-byte reproducible (ReportLab invariant mode), and every
manifest entry carries a stable import `key` that setup_demo.py uses to
recognise documents it has already uploaded. Keys are derived from the file
name; do not rename files of an instance that has already been seeded.

Usage (needs reportlab):  python make_docs.py out/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FACILITY = "Bluegum Ridge Care Community"
ADDRESS = "14 Bluegum Avenue, Ridgeview VIC 3999 (fictional)"

KEY_PREFIX = "carehub-demo:"

# Access levels map to groups in setup_demo.py
ALL, ADMIN, CLINICAL, FINANCE, FINANCE_ONLY = "all", "admin", "clinical", "finance", "finance_only"

DOCS: list[dict] = [
    {
        "file": "POL-FS-004 Fire Safety and Emergency Evacuation Policy v4.pdf",
        "title": "Fire Safety and Emergency Evacuation Policy v4",
        "created": "2026-08-01", "type": "Policy", "correspondent": FACILITY,
        "tags": ["Fire safety", "Emergency", "Current version"], "access": ALL,
        "meta": [("Document no.", "POL-FS-004"), ("Version", "4.0"), ("Effective", "1 August 2026"),
                 ("Next review", "1 August 2027"), ("Owner", "Facility Manager, Priya Raman"),
                 ("Replaces", "POL-FS-003 version 3 (2023)")],
        "body": [
            ("h", "1. Purpose"),
            ("p", "This policy sets out how staff at Bluegum Ridge Care Community prevent fires and respond to fire and other emergencies so that residents, visitors and staff are kept safe."),
            ("h", "2. What changed in version 4"),
            ("p", "The emergency assembly point has moved. Because of construction in the rear garden, the assembly point is now the <b>front car park, next to the Bluegum Avenue gate</b>. The old rear-garden assembly point must no longer be used."),
            ("h", "3. If you discover a fire: R.A.C.E."),
            ("p", "<b>R</b>emove anyone in immediate danger. <b>A</b>lert others: activate the nearest manual call point and call 000. <b>C</b>ontain the fire by closing doors. <b>E</b>xtinguish only if safe and trained, otherwise <b>E</b>vacuate."),
            ("h", "4. Key locations"),
            ("t", [["Item", "Location"],
                   ["Fire indicator panel", "Main reception, behind the front desk"],
                   ["Evacuation chairs", "Wing B stairwell, ground and first floor"],
                   ["Emergency resident list (printed)", "Red folder at the nurses' station, Wing A"],
                   ["Assembly point", "Front car park, next to the Bluegum Avenue gate"]]),
            ("h", "5. Fire wardens"),
            ("p", "A trained fire warden is rostered on every shift and is marked with (FW) on the weekly roster. The Chief Warden on weekdays is Priya Raman (ext. 201); after hours it is the Registered Nurse in charge."),
            ("h", "6. Drills and training"),
            ("p", "Evacuation drills are held every six months. The last drill was held on 5 August 2026 and the <b>next drill is scheduled for 3 February 2027</b>. All staff must complete annual fire safety training; completion is tracked in the Mandatory Training Matrix."),
        ],
    },
    {
        "file": "POL-FS-003 Fire Safety and Emergency Evacuation Policy v3 (SUPERSEDED).pdf",
        "title": "Fire Safety and Emergency Evacuation Policy v3 (superseded)",
        "created": "2023-05-15", "type": "Policy", "correspondent": FACILITY,
        "tags": ["Fire safety", "Emergency", "Superseded"], "access": ALL,
        "meta": [("Document no.", "POL-FS-003"), ("Version", "3.0"), ("Effective", "15 May 2023"),
                 ("Status", "SUPERSEDED by version 4 on 1 August 2026"), ("Owner", "Facility Manager")],
        "body": [
            ("h", "1. Purpose"),
            ("p", "This policy describes fire prevention and emergency evacuation at Bluegum Ridge Care Community."),
            ("h", "2. Assembly point"),
            ("p", "In an evacuation, all residents, visitors and staff gather at the <b>rear garden, beside the gazebo</b>."),
            ("h", "3. Drills"),
            ("p", "Evacuation drills are held once a year."),
        ],
    },
    {
        "file": "POL-IC-002 Infection Prevention and Control Policy.pdf",
        "title": "Infection Prevention and Control Policy v2",
        "created": "2026-03-10", "type": "Policy", "correspondent": FACILITY,
        "tags": ["Infection control", "Current version"], "access": ALL,
        "meta": [("Document no.", "POL-IC-002"), ("Version", "2.1"), ("Effective", "10 March 2026"),
                 ("Next review", "10 March 2027"), ("Owner", "IPC Lead, Daniel Okafor RN")],
        "body": [
            ("h", "1. Standard precautions"),
            ("p", "Hand hygiene is performed before and after every resident contact, using alcohol-based hand rub or soap and water. Gloves are not a substitute for hand hygiene."),
            ("h", "2. Outbreak management"),
            ("p", "An outbreak is suspected when two or more residents show similar respiratory or gastro symptoms within 72 hours. Notify the IPC Lead immediately (ext. 214) and the public health unit within 24 hours. During an outbreak, visitors are screened at reception and affected wings are closed to non-essential visits."),
            ("h", "3. PPE stock"),
            ("p", "PPE is stored in the clinical store room, Wing A. The IPC Lead checks stock every Monday and keeps a minimum of two weeks' supply."),
        ],
    },
    {
        "file": "POL-MM-005 Medication Management Policy.pdf",
        "title": "Medication Management Policy",
        "created": "2026-06-02", "type": "Policy", "correspondent": FACILITY,
        "tags": ["Medication", "Clinical", "Current version"], "access": ALL,
        "meta": [("Document no.", "POL-MM-005"), ("Version", "5.0"), ("Effective", "2 June 2026"),
                 ("Owner", "Director of Care, Helen Matthews")],
        "body": [
            ("p", "Only staff who have completed medication competency training may assist residents with medication. Competency is reassessed every 12 months."),
            ("p", "Medication errors and near misses are reported on the Incident Report Form within the same shift and escalated to the RN in charge. Schedule 8 medications are checked and signed by two staff, one of whom must be a nurse."),
            ("p", "The pharmacy supplier delivers on Tuesdays and Fridays. Urgent after-hours medication is ordered through the on-call pharmacy line listed at the nurses' station."),
        ],
    },
    {
        "file": "PRO-IR-001 Incident Reporting Procedure.pdf",
        "title": "Incident Reporting Procedure",
        "created": "2026-01-20", "type": "Procedure", "correspondent": FACILITY,
        "tags": ["Incidents", "Compliance", "Current version"], "access": ALL,
        "meta": [("Document no.", "PRO-IR-001"), ("Version", "3.0"), ("Effective", "20 January 2026"),
                 ("Owner", "Quality and Risk Coordinator, Sam Nguyen")],
        "body": [
            ("h", "1. What to report"),
            ("p", "All incidents, near misses and hazards involving residents, staff, visitors or contractors."),
            ("h", "2. Timeframes"),
            ("t", [["Type", "Report to manager", "External notification"],
                   ["Serious incident, Priority 1", "Immediately", "Within 24 hours (SIRS)"],
                   ["Serious incident, Priority 2", "Same shift", "Within 30 days (SIRS)"],
                   ["Other incident or near miss", "Before end of shift", "Not required"]]),
            ("h", "3. How to report"),
            ("p", "Complete the Incident Report Form (FORM-IR-01) and give it to the RN in charge. The Quality and Risk Coordinator reviews every report within two business days."),
        ],
    },
    {
        "file": "PRO-VS-002 Visitor and Contractor Sign-in Procedure.pdf",
        "title": "Visitor and Contractor Sign-in Procedure",
        "created": "2025-11-04", "type": "Procedure", "correspondent": FACILITY,
        "tags": ["Visitors", "Contractors", "Current version"], "access": ALL,
        "meta": [("Document no.", "PRO-VS-002"), ("Version", "2.0"), ("Effective", "4 November 2025")],
        "body": [
            ("p", "All visitors sign in on the reception kiosk and wear a visitor lanyard. Visiting hours are 9:00 am to 8:00 pm; outside these hours visitors ring the after-hours bell."),
            ("p", "Contractors must complete the Contractor Induction Checklist on their first visit, show current insurance, and be escorted while working in resident areas. Admin officers keep contractor insurance certificates on file and check expiry dates monthly."),
        ],
    },
    {
        "file": "POL-PR-001 Privacy and Confidentiality Policy.pdf",
        "title": "Privacy and Confidentiality Policy",
        "created": "2025-09-01", "type": "Policy", "correspondent": FACILITY,
        "tags": ["Privacy", "Current version"], "access": ALL,
        "meta": [("Document no.", "POL-PR-001"), ("Version", "1.3"), ("Effective", "1 September 2025")],
        "body": [
            ("p", "Resident and staff personal information is collected, used and stored in line with the Australian Privacy Principles. Information is only accessed by staff who need it for their role."),
            ("p", "Do not email resident information to personal email accounts or store it on USB drives. Printed records are locked away when not in use and shredded in the secure bins when no longer needed."),
            ("p", "Suspected privacy breaches are reported to the Facility Manager the same day."),
        ],
    },
    {
        "file": "PRO-CF-001 Complaints and Feedback Procedure.pdf",
        "title": "Complaints and Feedback Procedure",
        "created": "2026-02-14", "type": "Procedure", "correspondent": FACILITY,
        "tags": ["Complaints", "Current version"], "access": ALL,
        "meta": [("Document no.", "PRO-CF-001"), ("Version", "2.0"), ("Effective", "14 February 2026")],
        "body": [
            ("p", "Complaints can be made in person, by phone, by email or with the feedback forms at reception. Every complaint is acknowledged within 2 business days and the aim is to resolve it within 30 days."),
            ("p", "Admin officers log each complaint in the complaints register and pass it to the Facility Manager. Complainants are told they can also contact the Aged Care Quality and Safety Commission."),
        ],
    },
    {
        "file": "FORM-HR-03 Staff Leave Request Form.pdf",
        "title": "Staff Leave Request Form",
        "created": "2025-07-01", "type": "Form", "correspondent": FACILITY,
        "tags": ["HR", "Leave"], "access": ALL,
        "meta": [("Form no.", "FORM-HR-03"), ("Version", "2.0")],
        "body": [
            ("p", "Submit this form to your line manager at least <b>four weeks before</b> planned annual leave, or as soon as possible for personal leave. Approved forms are passed to the admin office for the roster and payroll."),
            ("t", [["Field", ""], ["Employee name", ""], ["Employee ID", ""], ["Leave type (annual / personal / long service / other)", ""],
                   ["First day of leave", ""], ["Last day of leave", ""], ["Manager approval and date", ""]]),
        ],
    },
    {
        "file": "FORM-IR-01 Incident Report Form.pdf",
        "title": "Incident Report Form",
        "created": "2026-01-20", "type": "Form", "correspondent": FACILITY,
        "tags": ["Incidents"], "access": ALL,
        "meta": [("Form no.", "FORM-IR-01"), ("Version", "3.0")],
        "body": [
            ("p", "Complete this form for every incident or near miss. See the Incident Reporting Procedure (PRO-IR-001) for timeframes."),
            ("t", [["Field", ""], ["Date and time of incident", ""], ["Location", ""], ["People involved", ""],
                   ["What happened", ""], ["Immediate action taken", ""], ["Reported by / signature", ""]]),
        ],
    },
    {
        "file": "FORM-CT-02 Contractor Induction Checklist.pdf",
        "title": "Contractor Induction Checklist",
        "created": "2025-11-04", "type": "Form", "correspondent": FACILITY,
        "tags": ["Contractors"], "access": ALL,
        "meta": [("Form no.", "FORM-CT-02")],
        "body": [
            ("t", [["Check", "Done"], ["Signed in at reception kiosk", ""], ["Public liability insurance sighted (expiry date recorded)", ""],
                   ["Fire assembly point shown (front car park, Bluegum Avenue gate)", ""], ["Infection control briefing", ""],
                   ["Escort arranged for resident areas", ""]]),
        ],
    },
    {
        "file": "Staff Roster - Week of 16 November 2026.pdf",
        "title": "Staff Roster - Week of 16 November 2026",
        "created": "2026-11-09", "type": "Roster", "correspondent": FACILITY,
        "tags": ["Roster"], "access": ALL,
        "meta": [("Period", "Monday 16 to Sunday 22 November 2026"), ("Prepared by", "Admin Office")],
        "body": [
            ("p", "(FW) = rostered fire warden for the shift."),
            ("t", [["Day", "Morning (7am-3pm)", "Afternoon (3pm-11pm)", "Night (11pm-7am)"],
                   ["Mon 16", "A. Silva RN (FW), J. Brown, M. Tan", "K. Lee RN (FW), R. Patel", "S. Ahmed RN (FW)"],
                   ["Tue 17", "A. Silva RN (FW), L. Garcia, M. Tan", "K. Lee RN (FW), T. Wilson", "S. Ahmed RN (FW)"],
                   ["Wed 18", "D. Okafor RN (FW), J. Brown, L. Garcia", "K. Lee RN (FW), R. Patel", "N. Kaur RN (FW)"],
                   ["Thu 19", "D. Okafor RN (FW), M. Tan, T. Wilson", "A. Silva RN (FW), R. Patel", "N. Kaur RN (FW)"],
                   ["Fri 20", "A. Silva RN (FW), J. Brown, L. Garcia", "K. Lee RN (FW), T. Wilson", "S. Ahmed RN (FW)"],
                   ["Sat 21", "N. Kaur RN (FW), M. Tan", "D. Okafor RN (FW), R. Patel", "S. Ahmed RN (FW)"],
                   ["Sun 22", "N. Kaur RN (FW), L. Garcia", "D. Okafor RN (FW), J. Brown", "S. Ahmed RN (FW)"]]),
        ],
    },
    {
        "file": "Staff Roster - Week of 23 November 2026.pdf",
        "title": "Staff Roster - Week of 23 November 2026",
        "created": "2026-11-16", "type": "Roster", "correspondent": FACILITY,
        "tags": ["Roster"], "access": ALL,
        "meta": [("Period", "Monday 23 to Sunday 29 November 2026"), ("Prepared by", "Admin Office"),
                 ("Note", "M. Tan on annual leave 23-27 November")],
        "body": [
            ("t", [["Day", "Morning (7am-3pm)", "Afternoon (3pm-11pm)", "Night (11pm-7am)"],
                   ["Mon 23", "A. Silva RN (FW), J. Brown, L. Garcia", "K. Lee RN (FW), R. Patel", "S. Ahmed RN (FW)"],
                   ["Tue 24", "D. Okafor RN (FW), J. Brown, T. Wilson", "K. Lee RN (FW), R. Patel", "S. Ahmed RN (FW)"],
                   ["Wed 25", "D. Okafor RN (FW), L. Garcia, T. Wilson", "A. Silva RN (FW), R. Patel", "N. Kaur RN (FW)"],
                   ["Thu 26", "A. Silva RN (FW), J. Brown, L. Garcia", "K. Lee RN (FW), T. Wilson", "N. Kaur RN (FW)"],
                   ["Fri 27", "N. Kaur RN (FW), J. Brown, T. Wilson", "D. Okafor RN (FW), R. Patel", "S. Ahmed RN (FW)"],
                   ["Sat 28", "N. Kaur RN (FW), M. Tan", "D. Okafor RN (FW), L. Garcia", "S. Ahmed RN (FW)"],
                   ["Sun 29", "A. Silva RN (FW), M. Tan", "K. Lee RN (FW), J. Brown", "S. Ahmed RN (FW)"]]),
        ],
    },
    {
        "file": "Invoice INV-20931 Coastal Linen Services October 2026.pdf",
        "title": "Invoice INV-20931 - Coastal Linen Services - October 2026",
        "created": "2026-11-02", "type": "Invoice", "correspondent": "Coastal Linen Services",
        "tags": ["Invoice", "Linen", "Awaiting approval"], "access": FINANCE,
        "meta": [("Invoice no.", "INV-20931"), ("Supplier", "Coastal Linen Services Pty Ltd (fictional)"),
                 ("Invoice date", "2 November 2026"), ("Due date", "2 December 2026"), ("PO number", "PO-4471")],
        "body": [
            ("t", [["Description", "Qty", "Amount"],
                   ["Linen hire and laundering - October 2026", "1", "$3,410.00"],
                   ["Replacement bath towels", "60", "$425.40"],
                   ["Subtotal", "", "$3,835.40"], ["GST", "", "$383.20"], ["Total due", "", "$4,218.60"]]),
        ],
    },
    {
        "file": "Invoice 7781 FreshPlate Food Supplies October 2026.pdf",
        "title": "Invoice 7781 - FreshPlate Food Supplies - October 2026",
        "created": "2026-11-03", "type": "Invoice", "correspondent": "FreshPlate Food Supplies",
        "tags": ["Invoice", "Catering", "Paid"], "access": FINANCE,
        "meta": [("Invoice no.", "7781"), ("Supplier", "FreshPlate Food Supplies (fictional)"),
                 ("Invoice date", "3 November 2026"), ("Due date", "17 November 2026"), ("Paid", "12 November 2026")],
        "body": [
            ("t", [["Description", "Amount"], ["Fresh produce, October 2026", "$5,120.10"], ["Meat and poultry, October 2026", "$4,870.25"],
                   ["Dairy and bakery, October 2026", "$2,950.00"], ["Total (GST-free food items)", "$12,940.35"]]),
        ],
    },
    {
        "file": "Invoice HP-5512 Harbour Plumbing hot water repair.pdf",
        "title": "Invoice HP-5512 - Harbour Plumbing - Wing A hot water repair",
        "created": "2026-10-16", "type": "Invoice", "correspondent": "Harbour Plumbing",
        "tags": ["Invoice", "Maintenance", "Paid"], "access": FINANCE,
        "meta": [("Invoice no.", "HP-5512"), ("Supplier", "Harbour Plumbing (fictional)"),
                 ("Invoice date", "16 October 2026"), ("Paid", "30 October 2026")],
        "body": [
            ("t", [["Description", "Amount"], ["Emergency call-out, Wing A hot water system", "$290.00"],
                   ["Replace tempering valve and labour (4 hrs)", "$1,410.00"], ["GST", "$170.00"], ["Total", "$1,870.00"]]),
        ],
    },
    {
        "file": "Service Report Harbour Plumbing TMV testing October 2026.pdf",
        "title": "Service Report - Thermostatic mixing valve testing - October 2026",
        "created": "2026-10-14", "type": "Service report", "correspondent": "Harbour Plumbing",
        "tags": ["Maintenance", "Compliance"], "access": ADMIN,
        "meta": [("Contractor", "Harbour Plumbing (fictional)"), ("Date of service", "14 October 2026"),
                 ("Next service due", "April 2027")],
        "body": [
            ("p", "All 38 thermostatic mixing valves (TMVs) were tested. 37 passed with outlet temperatures between 42 and 45 degrees C. The TMV in Wing A, room 9 ensuite failed (48 degrees C) and was replaced on 16 October 2026 (see invoice HP-5512)."),
            ("p", "The next six-monthly TMV test is due in <b>April 2027</b>."),
        ],
    },
    {
        "file": "Contract Coastal Linen Services Service Agreement 2025-2027.pdf",
        "title": "Service Agreement - Coastal Linen Services 2025-2027",
        "created": "2025-07-01", "type": "Contract", "correspondent": "Coastal Linen Services",
        "tags": ["Contract", "Linen"], "access": FINANCE,
        "meta": [("Term", "1 July 2025 to 30 June 2027"), ("Notice period", "90 days"),
                 ("Contract manager", "Facility Manager")],
        "body": [
            ("p", "Coastal Linen Services provides linen hire, laundering and delivery three times a week (Monday, Wednesday, Friday). Pricing is fixed until 30 June 2026 and then increases by CPI."),
            ("p", "Either party may end the agreement with 90 days' written notice. To avoid automatic renewal, notice must be given by <b>1 April 2027</b>."),
        ],
    },
    {
        "file": "Minutes Staff Meeting 28 October 2026.pdf",
        "title": "Staff Meeting Minutes - 28 October 2026",
        "created": "2026-10-28", "type": "Meeting minutes", "correspondent": FACILITY,
        "tags": ["Minutes"], "access": ADMIN,
        "meta": [("Chair", "Priya Raman, Facility Manager"), ("Attendees", "18 staff"), ("Minutes by", "Admin Office")],
        "body": [
            ("p", "<b>1. Fire safety.</b> The assembly point has moved to the front car park (policy v4). Old signs in the rear garden will be removed by 15 November. Action: maintenance."),
            ("p", "<b>2. Training.</b> Manual handling refresher is overdue for six staff. Sessions booked for 2 and 9 December. Action: admin office to send invites."),
            ("p", "<b>3. Document filing.</b> Staff reported difficulty finding the latest versions of policies on the shared drive. Admin office to propose a better system. Action: admin office."),
            ("p", "<b>4. Christmas function.</b> Resident and family Christmas lunch set for 18 December 2026."),
        ],
    },
    {
        "file": "Minutes Quality and Safety Committee September 2026.pdf",
        "title": "Quality and Safety Committee Minutes - September 2026",
        "created": "2026-09-22", "type": "Meeting minutes", "correspondent": FACILITY,
        "tags": ["Minutes", "Compliance"], "access": CLINICAL,
        "meta": [("Chair", "Helen Matthews, Director of Care"), ("Date", "22 September 2026")],
        "body": [
            ("p", "<b>Falls.</b> 9 falls in August (7 in July). Most occurred between 5 and 7 am. Action: trial sensor mats for high-risk residents in Wing A."),
            ("p", "<b>Medication incidents.</b> 3 minor incidents, no harm. Action: refresher on double-checking Schedule 8 medications."),
            ("p", "<b>Complaints.</b> 2 received, both resolved within 30 days."),
        ],
    },
    {
        "file": "Mandatory Training Matrix November 2026.pdf",
        "title": "Mandatory Training Matrix - November 2026",
        "created": "2026-11-10", "type": "Training record", "correspondent": FACILITY,
        "tags": ["Training", "Compliance"], "access": ADMIN,
        "meta": [("As at", "10 November 2026"), ("Staff on roster", "42")],
        "body": [
            ("t", [["Module", "Completed", "Compliance", "Overdue staff"],
                   ["Fire safety and evacuation", "40 / 42", "95%", "R. Patel, T. Wilson"],
                   ["Infection prevention and control", "42 / 42", "100%", "-"],
                   ["Manual handling", "36 / 42", "86%", "6 staff - sessions 2 and 9 December"],
                   ["Elder abuse and SIRS", "41 / 42", "98%", "L. Garcia"],
                   ["Medication competency (care staff only)", "15 / 16", "94%", "J. Brown"]]),
        ],
    },
    {
        "file": "Resident Care Plan Review Room 12.pdf",
        "title": "Resident Care Plan Review - Room 12 (Resident R-012)",
        "created": "2026-11-05", "type": "Resident record", "correspondent": FACILITY,
        "tags": ["Resident", "Care plan", "Confidential"], "access": CLINICAL,
        "meta": [("Resident", "R-012 (fictional)"), ("Room", "Wing A, Room 12"), ("Review date", "5 November 2026"),
                 ("Next review", "5 February 2027"), ("Reviewed by", "Helen Matthews RN")],
        "body": [
            ("p", "<b>Mobility and falls.</b> High falls risk (two falls in October). Sensor mat installed beside the bed on 1 November. Hourly checks between 5 and 7 am."),
            ("p", "<b>Nutrition.</b> Soft diet; weight stable at 61 kg."),
            ("p", "<b>Family contact.</b> Daughter (fictional) is the nominated representative and is updated after each review."),
        ],
    },
    {
        "file": "Resident Admission Checklist Room 7.pdf",
        "title": "Resident Admission Checklist - Room 7 (Resident R-007)",
        "created": "2026-10-20", "type": "Resident record", "correspondent": FACILITY,
        "tags": ["Resident", "Admission", "Confidential"], "access": CLINICAL,
        "meta": [("Resident", "R-007 (fictional)"), ("Admission date", "20 October 2026")],
        "body": [
            ("t", [["Item", "Status"], ["Resident agreement signed", "Done"], ["Medication chart received from GP", "Done"],
                   ["Advance care directive on file", "Outstanding - family to provide"], ["Initial care plan (within 21 days)", "Due 10 November 2026"],
                   ["Photo consent", "Done"]]),
        ],
    },
    {
        "file": "Budget Summary FY2026-27.pdf",
        "title": "Budget Summary FY2026-27",
        "created": "2026-06-20", "type": "Budget", "correspondent": FACILITY,
        "tags": ["Budget", "Confidential"], "access": FINANCE_ONLY,
        "meta": [("Financial year", "1 July 2026 to 30 June 2027"), ("Approved", "20 June 2026")],
        "body": [
            ("t", [["Category", "Budget"], ["Staffing (wages and on-costs)", "$6,480,000"], ["Catering", "$160,000"],
                   ["Linen and laundry", "$52,000"], ["Maintenance and repairs", "$95,000"], ["Training", "$28,000"],
                   ["IT and software", "$18,000"]]),
            ("p", "IT and software spending to date is $9,400. Any new software subscription over $2,000 per year needs Facility Manager approval."),
        ],
    },
]


def import_key(doc: dict) -> str:
    return KEY_PREFIX + Path(doc["file"]).stem


def build(doc: dict, out: Path) -> None:
    styles = getSampleStyleSheet()
    story = [
        Paragraph(FACILITY, styles["Normal"]),
        Paragraph(ADDRESS, styles["Italic"]),
        Spacer(1, 4 * mm),
        Paragraph(doc["title"], styles["Title"]),
    ]
    meta = Table([[k, v] for k, v in doc["meta"]], colWidths=[40 * mm, 120 * mm])
    meta.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [meta, Spacer(1, 5 * mm)]
    for kind, content in doc["body"]:
        if kind == "h":
            story.append(Paragraph(content, styles["Heading3"]))
        elif kind == "p":
            story += [Paragraph(content, styles["BodyText"]), Spacer(1, 2 * mm)]
        elif kind == "t":
            table = Table(content, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f5f5b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story += [table, Spacer(1, 3 * mm)]

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Oblique", 7.5)
        canvas.setFillColor(colors.HexColor("#9a3412"))
        canvas.drawString(18 * mm, 10 * mm, "FICTIONAL DEMO DOCUMENT - invented for a software demonstration. Not real people, residents or data.")
        canvas.restoreState()

    SimpleDocTemplate(str(out / doc["file"]), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=18 * mm, title=doc["title"], invariant=1).build(
        story, onFirstPage=footer, onLaterPages=footer)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0] if argv else "out")
    out.mkdir(parents=True, exist_ok=True)
    for doc in DOCS:
        build(doc, out)
    manifest = [{"key": import_key(d), **{k: d[k] for k in ("file", "title", "created", "type", "correspondent",
                                                             "tags", "access")}} for d in DOCS]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(DOCS)} PDFs to {out}")


if __name__ == "__main__":
    main()
