// Thin front door for the static build on Cloudflare Workers.
// The build writes .html files and links to them explicitly, so assets are served exactly as
// generated (html_handling: "none"); this only maps the bare root onto index.html and turns an
// unknown path into the atlas 404 page instead of an empty response.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    let path = url.pathname;
    if (path === "/" || path === "") {
      return env.ASSETS.fetch(new Request(new URL("/index.html", url), request));
    }
    const res = await env.ASSETS.fetch(request);
    if (res.status === 404) {
      const nf = await env.ASSETS.fetch(new Request(new URL("/404.html", url), request));
      if (nf.status === 200) return new Response(nf.body, {status: 404, headers: nf.headers});
    }
    return res;
  },
};
