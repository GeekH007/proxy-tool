const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');
const { startProxy } = require('../../lan-forward-proxy/test-support');
const { startWorker } = require('../benchmark');

test('V2 with real upstreams: CONNECT bytes, half-close, keep-alive and forced upstream outage',
  { timeout: 30000 }, async () => {
    const proxies = [];
    let worker;
    const payload = Buffer.alloc(1024 * 1024);
    for (let i = 0; i < payload.length; i++) payload[i] = i % 251;
    const echo = net.createServer({ allowHalfOpen: true }, socket => {
      socket.on('error', () => {});
      const chunks = [];
      socket.on('data', data => chunks.push(data));
      socket.on('end', () => socket.end(Buffer.concat(chunks)));
    });
    const origin = http.createServer((_req, res) => {
      res.writeHead(200, { 'Content-Length': 2 });
      res.end('ok');
    });
    let agent;
    try {
      echo.listen(0, '127.0.0.1');
      origin.listen(0, '127.0.0.1');
      await Promise.all([once(echo, 'listening'), once(origin, 'listening')]);
      proxies.push(await startProxy());
      proxies.push(await startProxy());
      worker = await startWorker('proxy_tool.py', proxies.map(p => ['127.0.0.1', p.ports.http]), 0);
      await new Promise((done, fail) => {
        const req = http.request({ host: '127.0.0.1', port: worker.port,
          method: 'CONNECT', path: `127.0.0.1:${echo.address().port}` });
        req.on('error', fail);
        req.on('connect', (response, socket, head) => {
          try { assert.equal(response.statusCode, 200); } catch (error) { socket.destroy(); fail(error); return; }
          const chunks = [head];
          socket.setTimeout(5000, () => socket.destroy(new Error('Tunnel timeout')));
          socket.on('error', fail);
          socket.on('data', chunk => chunks.push(chunk));
          socket.on('end', () => {
            try { assert.deepEqual(Buffer.concat(chunks), payload); done(); }
            catch (error) { fail(error); }
          });
          socket.end(payload);
        });
        req.end();
      });
      agent = new http.Agent({ keepAlive: true, maxSockets: 1 });
      async function request(useAgent) {
        return new Promise((done, fail) => {
          const req = http.get({ host: '127.0.0.1', port: worker.port, agent: useAgent,
            path: `http://127.0.0.1:${origin.address().port}/` }, res => {
            let text = '';
            res.on('data', data => { text += data; });
            res.on('error', fail);
            res.on('end', () => {
              try { assert.equal(res.statusCode, 200); assert.equal(text, 'ok'); done(req.socket); }
              catch (error) { fail(error); }
            });
          });
          req.on('error', fail);
          req.setTimeout(8000, () => req.destroy(new Error('Request timeout')));
        });
      }
      const firstSocket = await request(agent);
      assert.equal(await request(agent), firstSocket);
      agent.destroy();
      await proxies[0].stop();
      await Promise.all(Array.from({ length: 16 }, () => request(false)));
    } finally {
      agent?.destroy();
      if (worker) await worker.stop();
      await Promise.all(proxies.map(p => p.stop()));
      origin.closeAllConnections();
      await Promise.all([new Promise(done => echo.close(done)), new Promise(done => origin.close(done))]);
    }
  });
