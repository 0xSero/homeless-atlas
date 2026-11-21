# Public records laws (records_laws)

How the atlas knows what each state's public records law says, and how sure it is.

## Method

- **Scope.** One entry per jurisdiction in `ingest/records_laws.yaml`: the 50 states, DC, and US (the federal FOIA).
- **What each entry holds.** Who may request, the response deadline, written-request and form rules, fees, fee waiver, what silence means (deemed denial), and the appeal route.
- **Federal extras.** The US entry adds the FOIA fee categories, the fee waiver at 5 U.S.C. 552(a)(4)(A)(iii), FOIA routes and appeal routes for HUD, VA and HHS, and HUD's Continuum of Care public posting rule.
- **Every legal fact has a quote.** Each fact carries an exact quote under 40 words and the official URL it came from.
- **Sources.** The first choice is the legislature's or official code site. When that site cannot be fetched without a browser, or the official code is hosted only by a commercial publisher, the entry uses the attorney general's or open records office's own guide (`basis: official_guide`) and says why in `basis_note`. Justia, FindLaw and Casetext are never used.
- **The verifier.** `python3 -m ingest.domains.records_laws` fetches every `quote_url` through `ingest.fetch_ladder` and normalizes the text. Normalizing means: Unicode NFKC, quote marks and dashes made plain, section signs dropped, and whitespace collapsed. It then requires each quote to appear verbatim. It writes `records_law` (one row per jurisdiction: the entry with per-field `verified`, `via` and `verified_at`) and `records_law_check` (one row per field). It exits non-zero if any `who_may_request` or `response` field fails or any jurisdiction is missing. `--dry` runs against an in-memory DB.
- **Letters use only verified facts.** `ingest/records_requests.py` puts only verified fields into letters: an unverified fact is left out, not guessed. Deadlines that apply only to state agencies (`scope: state_agencies`, Alaska) are left out of letters to local bodies.
- **Appeal URLs.** An appeal URL stays in only if it loaded at verification time. Massachusetts (blocked), Illinois and Mississippi (only in the Wayback Machine) were set to null.

## Coverage

Run 2026-11-20: 52 of 52 jurisdictions, 272 of 272 quoted fields verified, exit 0. Every `who_may_request` and `response` field verified.

Column notes:
- **Response.** A "no fixed days" response means the law says promptly or within a reasonable time. The letters then state no number, and `deadline_calc` is null.
- **Appeal.** "not verified" means no appeal route is recorded. The letter generator then writes no appeal letter.

| Jur | Law | Who may request | Response | Deemed denial | Appeal | Basis | Verified |
|---|---|---|---|---|---|---|---|
| AK | Alaska Public Records Act | any_person | 10 business (state agencies) | not stated | agency head (state agencies, within 60 working days), or superior court | statute | 8/8 |
| AL | Alabama Open Records Act | other | no fixed days | not stated | court | statute | 2/2 |
| AR | Arkansas Freedom of Information Act | state_residents_only | 3 business | not stated | court (circuit court) | official_guide | 5/5 |
| AZ | Arizona Public Records Law | any_person | no fixed days | yes | court (special action in superior court) | statute | 7/7 |
| CA | California Public Records Act | any_person | 10 calendar | not stated | court | statute | 5/5 |
| CO | Colorado Open Records Act | any_person | 3 business | not stated | court (district court), after 14 days' written notice to the custodian | statute | 6/6 |
| CT | Freedom of Information Act | any_person | 4 business | yes | Connecticut Freedom of Information Commission | statute | 9/9 |
| DC | D.C. Freedom of Information Act | any_person | 15 business | yes | Mayor of the District of Columbia (administrative appeal), or Superior Court | statute | 6/6 |
| DE | Delaware Freedom of Information Act | other | 15 business | not stated | Delaware Attorney General (petition), or court within 60 days | statute | 6/6 |
| FL | Florida Public Records Act | any_person | no fixed days | not stated | court (circuit court, with an immediate hearing) | statute | 5/5 |
| GA | Georgia Open Records Act | any_person | 3 business | not stated | court (civil action) | official_guide | 6/6 |
| HI | Hawaii public records (UIPA) | any_person | 10 business | not stated | Hawaii Office of Information Practices | statute | 3/3 |
| IA | Iowa Open Records Law | any_person | no fixed days | not stated | Iowa Public Information Board (complaint), or court | statute | 5/5 |
| ID | Idaho Public Records Act | any_person | 3 business | yes | court (district court petition) | statute | 7/7 |
| IL | Freedom of Information Act | any_person | 5 business | yes | Public Access Counselor, Office of the Illinois Attorney General | statute | 8/8 |
| IN | Access to Public Records Act | any_person | no fixed days | yes | Indiana Public Access Counselor (formal complaint) | official_guide | 6/6 |
| KS | Kansas Open Records Act | any_person | 3 business | not stated | not verified | statute | 4/4 |
| KY | Kentucky Open Records Act | state_residents_only | 5 business | not stated | Kentucky Attorney General (open records appeal), or circuit court | statute | 8/8 |
| LA | Louisiana Public Records Law | other | 5 business | yes | court (district court) | statute | 6/6 |
| MA | Massachusetts Public Records Law | any_person | 10 business | not stated | Supervisor of Records (Secretary of the Commonwealth), or superior court | statute | 5/5 |
| MD | Maryland Public Information Act | any_person | 30 calendar | not stated | not verified | statute | 3/3 |
| ME | Maine Freedom of Access Act | any_person | 5 business | not stated | court (Superior Court) | statute | 5/5 |
| MI | Michigan Freedom of Information Act | any_person | 5 business | yes | head of the public body (written appeal stating the word "appeal"), or circuit court | statute | 7/7 |
| MN | Minnesota Government Data Practices Act | any_person | no fixed days | not stated | not verified | statute | 3/3 |
| MO | Missouri Sunshine Law | any_person | 3 business | not stated | not verified | statute | 3/3 |
| MS | Mississippi Public Records Act | any_person | 7 business | not stated | Mississippi Ethics Commission (complaint), or chancery court | official_guide | 4/4 |
| MT | Montana Public Records Act | any_person | no fixed days | not stated | not verified | statute | 3/3 |
| NC | North Carolina Public Records Law | any_person | no fixed days | not stated | not verified | statute | 3/3 |
| ND | North Dakota Open Records Law | any_person | no fixed days | not stated | North Dakota Attorney General (opinion review) | statute | 5/5 |
| NE | Nebraska public records | any_person | 4 business | not stated | not verified | statute | 4/4 |
| NH | Right-to-Know Law | other | 5 business | not stated | not verified | statute | 3/3 |
| NJ | Open Public Records Act | other | 7 business | yes | Government Records Council (complaint), or Superior Court | official_guide | 5/5 |
| NM | Inspection of Public Records Act | any_person | 15 calendar | yes | not verified | official_guide | 3/3 |
| NV | Nevada Public Records Act | any_person | 5 business | not stated | court (district court) | statute | 4/4 |
| NY | Freedom of Information Law | any_person | 5 business | yes | head, chief executive or governing body of the agency | statute | 4/4 |
| OH | Ohio Public Records Act | any_person | no fixed days | not stated | not verified | statute | 4/4 |
| OK | Oklahoma Open Records Act | any_person | no fixed days | not stated | not verified | statute | 3/3 |
| OR | Oregon Public Records Law | any_person | 5 business | not stated | District attorney of the county (local bodies) or Attorney General (state agencies) | statute | 4/4 |
| PA | Right-to-Know Law | other | 5 business | yes | Pennsylvania Office of Open Records | statute | 10/10 |
| RI | Access to Public Records Act | any_person | 10 business | yes | chief administrative officer of the public body, then the Attorney General | statute | 8/8 |
| SC | South Carolina Freedom of Information Act | any_person | 10 business | approved | court (circuit court | statute | 7/7 |
| SD | South Dakota Public Records Law | any_person | 10 business | yes | Office of Hearing Examiners (written notice of review), or circuit court | statute | 5/5 |
| TN | Tennessee Public Records Act | state_residents_only | 7 business | not stated | court (circuit or chancery court petition) | official_guide | 5/5 |
| TX | Texas Public Information Act | any_person | 10 business | presumed public | not verified | official_guide | 5/5 |
| UT | Utah GRAMA | any_person | 10 business | not stated | chief administrative officer of the governmental entity | statute | 5/5 |
| VA | Virginia Freedom of Information Act | state_residents_only | 5 business | yes | not verified | statute | 5/5 |
| VT | Vermont Public Records Act | any_person | 3 business | yes | head of the agency | statute | 4/4 |
| WA | Washington Public Records Act | any_person | 5 business | not stated | court (superior court) | statute | 3/3 |
| WI | Wisconsin Public Records Law | any_person | no fixed days | not stated | court (mandamus), the Attorney General, or the district attorney | statute | 5/5 |
| WV | West Virginia Freedom of Information Act | any_person | 5 business | not stated | court (circuit court of the county where the record is kept) | statute | 4/4 |
| WY | Wyoming Public Records Act | any_person | 30 calendar | not stated | Public Records Ombudsman (complaint) | statute | 4/4 |
| US | Freedom of Information Act | any_person | 20 business | yes | head of the agency (administrative appeal) | statute | 15/15 |

## Residents-only and citizen-language states

Classified `state_residents_only`. Letters carry a caution and, where the law lets the agency ask, a residency statement:

- **Arkansas**, Ark. Code Ann. 25-19-105: "by any citizen of the State of Arkansas" (Attorney General's FOIA Handbook, 22nd ed., official guide).
- **Kentucky**, KRS 61.872(1): "open for inspection by any resident of the Commonwealth"; KRS 61.870(10) defines resident.
- **Tennessee**, Tenn. Code Ann. 10-7-503(a)(2)(A): "Only citizens of Tennessee have the right to inspect" (Comptroller's Office of Open Records Counsel FAQ, official guide).
- **Virginia**, Va. Code 2.2-3704(A): "open to citizens of the Commonwealth" and in-state media.

Classified `other`, with a caution because the statute speaks of citizens without saying state citizens or residents:
- Alabama (36-12-40, "Every citizen")
- Delaware (29 Del. C. 10003, "any citizen")
- New Hampshire (RSA 91-A:4, "Every citizen")
- New Jersey (N.J.S.A. 47:1A-1, "the citizens of this State", in the policy section)

Also `other`:
- Pennsylvania: the requester must be a legal resident of the United States, 65 P.S. 67.102.
- Louisiana: any person of the age of majority, La. R.S. 44:31.

Indiana is `any_person`, with a caution about the out-of-state supplemental fee added by HEA 1360.

## Known gaps

- **No fixed response deadline.** The law says promptly or reasonable time in AL, AZ, FL, IN, IA, MN, MT, NC, ND, OH, OK and WI. Alaska's 10 working days apply to state agencies only.
- **No appeal route recorded.** KS, MD, MN, MO, MT, NE, NH, NM, NC, OH, OK, TX and VA. These states either have no administrative appeal (the remedy is a court case) or the provision was not fetched and verified. Letters for these states include no appeal letter.
- **Court-only remedies.** In CA, LA, NV, TN, WA, WV and SC the appeal is a court case, so the generator writes no appeal letter and says so. SC's court remedy is limited to South Carolina citizens.
- **Official guides instead of code text.** AR, GA, IN, MS, NJ, NM, TN and TX use an official guide. The reasons:
  - The official code site needs JavaScript (NJ, NM, TX).
  - The code is published only through a commercial publisher's site (MS, TN, GA).
  - The handbook was the fetchable official text (AR, IN).
- **Field-level gaps.** Fees, fee waiver and deemed denial are recorded only where a provision was fetched and quoted. A null field means not recorded, not that the law is silent.
- **HUD CoC posting.** `hud_coc_public_posting` comes from HUD's FY 2026 CoC Application Detailed Instructions (NOFO V.B.1.a.(2), question 1D-2a). It is a scoring rule in the competition, not a statute or regulation, and letters describe it that way.
- **VA and HHS FOIA routes.** The VA FOIA page lists a portal but no mailing address, so the VA letter is portal-only. HHS appeals follow the instructions in each response letter, so the HHS appeal deadline is null.
- **CoC lead agencies.** The database does not record CoC lead agencies. CoC letters tell the user to find the lead agency, and they apply state law only "if your agency is a public body".
- **`docs/SOURCES.md`.** No records_laws block was appended. The file is append-only under the ingest contract and outside this task's file scope. Source rows are written to the `source` table by the verifier.

## Verified

Five hand spot checks, 2026-11-20. Each one reads the raw fetched text (not the verifier's normalized string) around the quote:

1. **PA response deadline.** palegis.us Act 3 of 2008 shows "The time for response shall not exceed five business days from the date the written request is received by the open-records officer for an agency." The next sentence is the deemed-denial rule. PASS.
2. **KY residents only.** apps.legislature.ky.gov statute id 51391 (KRS 61.872) shows the heading "Right of Kentucky residents to inspect public records" followed by "All public records shall be open for inspection by any resident of the Commonwealth". PASS.
3. **TN citizens only.** The Comptroller's Public Records Act FAQ answers "Who can request public records?" with "Only citizens of Tennessee have the right to inspect and receive copies of public records under the Tennessee Public Records Act. Tenn. Code Ann. 10-7-503(a)(2)(A)." PASS.
4. **VA citizens and in-state media.** law.lis.virginia.gov 2.2-3704(A) shows "all public records shall be open to citizens of the Commonwealth, representatives of newspapers and magazines with circulation in the Commonwealth". PASS.
5. **SC silence means approval.** scstatehouse.gov Title 30 Chapter 4 (30-4-30(C)) shows that if the determination is not delivered in time, "the request must be considered approved as to nonexempt records or information". This is the opposite of most states, and the follow-up letter says so. PASS.
