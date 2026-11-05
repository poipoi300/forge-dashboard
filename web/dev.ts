// Development server: Bun's HTML bundler with HMR, proxying /api (HTTP and the
// event WebSocket) to the API the runner started (API_URL).
import index from "./src/index.html";

const apiUrl = process.env.API_URL;
if (!apiUrl) throw new Error("API_URL is required (the runner sets it from the api service)");
const api = new URL(apiUrl);

type Upstream = { upstream: WebSocket | null; pending: string[] };

const server = Bun.serve<Upstream>({
  hostname: process.env.HOST ?? "127.0.0.1",
  port: Number(process.env.PORT ?? 0),
  development: { hmr: true, console: true },
  routes: {
    "/api/events": (request: Request, bunServer: Bun.Server<Upstream>) => {
      if (bunServer.upgrade(request, { data: { upstream: null, pending: [] } })) return undefined;
      return new Response("expected a WebSocket upgrade", { status: 426 });
    },
    "/api/*": (request: Request) => {
      const url = new URL(request.url);
      const target = new URL(url.pathname + url.search, api);
      return fetch(target, {
        method: request.method,
        headers: request.headers,
        body: request.body,
      });
    },
    "/*": index,
  },
  websocket: {
    open(socket) {
      const target = new URL("/api/events", api);
      target.protocol = api.protocol === "https:" ? "wss:" : "ws:";
      const upstream = new WebSocket(target);
      socket.data.upstream = upstream;
      upstream.onopen = () => {
        for (const message of socket.data.pending) upstream.send(message);
        socket.data.pending = [];
      };
      upstream.onmessage = (event) => socket.send(String(event.data));
      upstream.onclose = () => socket.close();
    },
    message(socket, message) {
      const upstream = socket.data.upstream;
      const text = String(message);
      if (upstream?.readyState === WebSocket.OPEN) upstream.send(text);
      else socket.data.pending.push(text);
    },
    close(socket) {
      socket.data.upstream?.close();
    },
  },
});

console.log(`dev server on ${server.url} (API ${api.origin})`);
