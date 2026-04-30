// Production deployments override ALLOWED_ORIGIN via the AQUARIUM_ORIGIN worker env var.
const ALLOWED_ORIGIN = (typeof AQUARIUM_ORIGIN !== 'undefined' && AQUARIUM_ORIGIN)
    ? AQUARIUM_ORIGIN
    : 'https://your-org.github.io';
const KV_KEY = 'aquarium';

export default {
  async fetch(request, env) {
    const cors = {
      'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
      'Access-Control-Allow-Methods': 'GET, PUT, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Tealc-Auth',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }

    const url = new URL(request.url);
    if (url.pathname !== '/') {
      return new Response('not found', { status: 404, headers: cors });
    }

    if (request.method === 'GET') {
      const data = await env.AQUARIUM.get(KV_KEY) || '{"last_updated":null,"recent_activity":[]}';
      return new Response(data, {
        headers: {
          ...cors,
          'Content-Type': 'application/json',
          'Cache-Control': 'no-store',
        },
      });
    }

    if (request.method === 'PUT') {
      const auth = request.headers.get('X-Tealc-Auth');
      if (!auth || auth !== env.AQUARIUM_SECRET) {
        return new Response('forbidden', { status: 403, headers: cors });
      }
      const body = await request.text();
      try {
        JSON.parse(body);
      } catch {
        return new Response('invalid json', { status: 400, headers: cors });
      }
      if (body.length > 100_000) {
        return new Response('payload too large', { status: 413, headers: cors });
      }
      await env.AQUARIUM.put(KV_KEY, body);
      return new Response('ok', { headers: cors });
    }

    return new Response('not found', { status: 404, headers: cors });
  },
};
