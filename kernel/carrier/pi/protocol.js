import { randomUUID } from "node:crypto";
import { createConnection } from "node:net";

const MAX_FRAME_BYTES = 16 * 1024 * 1024;

function sortedValue(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(sortedValue);
  if (typeof value === "object" && value.constructor === Object) {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) result[key] = sortedValue(value[key]);
    }
    return result;
  }
  throw new TypeError("model request must contain only finite JSON values");
}

export function canonicalJson(value) {
  return JSON.stringify(sortedValue(value));
}

export class AisaClient {
  constructor(socketPath = process.env.CASTOR_IPC_SOCKET || "/run/castor/ipc.sock") {
    this.socketPath = socketPath;
  }

  request(op, payload) {
    const requestId = randomUUID();
    const body = Buffer.from(JSON.stringify({ request_id: requestId, op, payload }));
    if (body.length === 0 || body.length > MAX_FRAME_BYTES) {
      return Promise.reject(new Error("AISA request exceeds frame limit"));
    }
    const header = Buffer.alloc(4);
    header.writeUInt32BE(body.length);
    return new Promise((resolve, reject) => {
      const socket = createConnection({ path: this.socketPath });
      let received = Buffer.alloc(0);
      let settled = false;
      const fail = (error) => {
        if (settled) return;
        settled = true;
        socket.destroy();
        reject(error);
      };
      socket.setTimeout(5000, () => fail(new Error("AISA response timed out")));
      socket.on("error", fail);
      socket.on("end", () => fail(new Error("AISA response ended before frame completed")));
      socket.on("connect", () => socket.write(Buffer.concat([header, body])));
      socket.on("data", (chunk) => {
        if (settled) return;
        received = Buffer.concat([received, chunk]);
        if (received.length < 4) return;
        const length = received.readUInt32BE(0);
        if (length === 0 || length > MAX_FRAME_BYTES) {
          fail(new Error("invalid AISA response frame length"));
          return;
        }
        if (received.length < length + 4) return;
        try {
          const response = JSON.parse(received.subarray(4, length + 4).toString("utf8"));
          if (response.request_id !== requestId) throw new Error("AISA response ID mismatch");
          if (response.status !== "Ok") throw new Error(`AISA ${response.error?.code || "error"}`);
          settled = true;
          socket.destroy();
          resolve(response.outcome);
        } catch (error) {
          fail(error);
        }
      });
    });
  }
}
