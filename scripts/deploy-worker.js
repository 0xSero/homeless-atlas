// Thin front door for the static build on Cloudflare Workers.
// The build emits .html files and links to them explicitly (html_handling: "none"), so this only
// maps the bare root onto index.html and hands unknown paths the atlas 404 page.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "") {
      return env.ASSETS.fetch(new Request(new URL("/index.html", url), request));
    }
    const res = await env.ASSETS.fetch(request);
    if (res.status !== 404) return res;
    try {
      const nf = await env.ASSETS.fetch(new URL("/404.html", url));
      if (nf.status === 200) {
        return new Response(nf.body, {
          status: 404,
          headers: {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        });
      }
    } catch (e) { /* fall through to the plain response */ }
    return new Response("Not found\n", {status: 404, headers: {"content-type": "text/plain; charset=utf-8"}});
  },
};
