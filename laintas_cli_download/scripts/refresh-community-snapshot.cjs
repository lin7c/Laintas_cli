// Refresh the community-registry snapshot baked into the static build, so
// the prerendered /plugins page shows real community data (the prerender
// server cannot reach the live API). Fail-soft: a stale snapshot beats none.
fetch('https://cli.laintas.com/api/extensions/community').then(r=>r.json()).then(j=>require('fs').writeFileSync('public/extensions/community-snapshot.json',JSON.stringify(j))).then(()=>console.log('snapshot ok')).catch(()=>console.log('snapshot refresh skipped'))
