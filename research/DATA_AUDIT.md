
## 2026-11-19 — impossible values removed

`chronic > total` in the HUD PIT series (a subpopulation cannot exceed the whole). Values were set to NULL rather than published, and nothing was estimated in their place:

- PR-502 2008: total 707, published chronic 1488
- TN-500 2008: total 87, published chronic 679
- PR-502 2011: total 1362, published chronic 1717
- PR-503 2011: total 1538, published chronic 1847
- PR-503 2012: total 1527, published chronic 1684

Placeholder recipient names carried by federal awards (the source literally says `TBD`) are kept as-is but flagged `org.status='placeholder_name'` so they never rank as real organisations.

Negative award amounts (877 rows) are legitimate deobligations, not errors; they are kept and shown.

### Institution dates that contradict themselves

Flagged `status='unknown'` and the impossible `closed` year removed (not silently changed):

- Wyoming State Penitentiary (397): source said opened 2001, closed 1980

### audit --fix pass

- pit PR-502 2008: chronic 1488 > total 707 -> NULL
- pit TN-500 2008: chronic 679 > total 87 -> NULL
- pit PR-502 2011: chronic 1717 > total 1362 -> NULL
- pit PR-503 2011: chronic 1847 > total 1538 -> NULL
- pit PR-503 2012: chronic 1684 > total 1527 -> NULL
- facility Wyoming State Penitentiary (396): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- pit PR-502 2008: chronic 1488 > total 707 -> NULL
- pit TN-500 2008: chronic 679 > total 87 -> NULL
- pit PR-502 2011: chronic 1717 > total 1362 -> NULL
- pit PR-503 2011: chronic 1847 > total 1538 -> NULL
- pit PR-503 2012: chronic 1684 > total 1527 -> NULL
- facility Wyoming State Penitentiary (396): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- geo_state: repointed 51 population rows to the population source

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown

### audit --fix pass

- facility Wyoming State Penitentiary (378): closed 1980 < opened 2001 -> closed NULL, status unknown
