/**
 * A minimal stand-in for `XMLHttpRequest` for tests: it records the request
 * and lets the test drive upload progress, the response, errors, and aborts.
 * Install it with `vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest)`.
 */

type Handler = (() => void) | null;
type ProgressHandler = ((event: ProgressEvent) => void) | null;

export class FakeXMLHttpRequest {
  static instances: FakeXMLHttpRequest[] = [];

  static reset() {
    FakeXMLHttpRequest.instances = [];
  }

  /** The most recently created request. */
  static latest(): FakeXMLHttpRequest {
    const request = FakeXMLHttpRequest.instances.at(-1);
    if (!request) {
      throw new Error("No XMLHttpRequest was created.");
    }
    return request;
  }

  method = "";
  url = "";
  withCredentials = false;
  requestHeaders: Record<string, string> = {};
  body: unknown = undefined;
  sent = false;
  aborted = false;
  status = 0;
  responseText = "";

  upload: { onprogress: ProgressHandler; onload: Handler } = { onprogress: null, onload: null };
  onload: Handler = null;
  onerror: Handler = null;
  onabort: Handler = null;

  private done = false;

  constructor() {
    FakeXMLHttpRequest.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.requestHeaders[name] = value;
  }

  send(body: unknown) {
    this.body = body;
    this.sent = true;
  }

  abort() {
    if (this.done) {
      return;
    }
    this.done = true;
    this.aborted = true;
    this.onabort?.();
  }

  // Test controls.

  progress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as ProgressEvent);
  }

  finishSending() {
    this.upload.onload?.();
  }

  respond(status: number, body?: unknown) {
    this.done = true;
    this.status = status;
    this.responseText =
      body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
    this.onload?.();
  }

  failNetwork() {
    this.done = true;
    this.onerror?.();
  }
}
