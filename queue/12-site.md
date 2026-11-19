# site — the searchable wiki with maps (I own this item)
Build scripts/build_site.py -> site/ output: index (heat map + KPIs), /states, /state/<abbr>,
/coc/<num>, /city/<geoid>, /org/<slug>, /person/<slug>, /facilities, /laws, /budgets, /media, /docs,
/search, /about, /method, /sources, /report/san-francisco. MapLibre GL JS, no build step, no server,
search-index.json + client fuzzy search, keyboard nav, mobile-first CSS, banners per section.
accept: every entity reachable, 0 dead internal links, renders at 390 and 1440, search returns hits
