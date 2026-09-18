import type { QuoteTime } from '../providers/pricebook.ts';
import type { ModelSpec } from './types.ts';
import type { Request, Response, StreamEvent, Usage } from '../protocol/open-responses/index.ts';
import type { ContextRecord, ItemOrigin } from '../protocol/open-responses/context.ts';

/** Missing meters remain null. Native details are retained for future reconciliation. */
export interface TokenMeters {
  input: number | null;
  output: number | null;
  total: number | null;
  cachedInput: number | null;
  uncachedInput: number | null;
  reasoning: number | null;
  details?: Record<string, { quantity: number | null; unit: string }>;
  native: Record<string, unknown> | null;
}
export type Meter = Exclude<keyof TokenMeters, 'native' | 'details'> | `detail:${string}`;
export interface PriceRule { meter: Meter; perMillion: number; unit?: string; }
export interface PriceSnapshot {
  id: string;
  currency: string;
  basis: 'marginal' | 'equivalent';
  rules: PriceRule[];
  inputBands?: Array<{ from: number; rules: PriceRule[] }>;
  serviceTiers?: Record<string, { rules: PriceRule[]; inputBands?: Array<{ from: number; rules: PriceRule[] }> }>;
  source: string;
  capturedAt: string;
}
export interface Charge {
  quote: PriceSnapshot;
  amount: number | null;
  knownAmount: number;
  missing: Array<Meter | 'serviceTier'>;
  lines: Array<{ meter: Meter; unit: string; quantity: number | null; perMillion: number; amount: number | null }>;
}
export interface ProviderAttempt {
  id: string;
  generationId: string;
  ordinal: number;
  origin: ItemOrigin;
  startedAt: string;
  elapsedMs: number;
  requestId: string | null;
  responseId: string | null;
  outcome: 'completed' | 'incomplete' | 'failed' | 'aborted' | 'discarded';
  status: number | null;
  serviceTier: string | null;
  requestedServiceTier?: string | null;
  purpose?: 'generation' | 'diagnostic';
  meters: TokenMeters;
  charges: Charge[];
}
export interface GenerateOptions {
  signal?: AbortSignal;
  onEvent?: (event: StreamEvent) => void;
  role?: string;
  sessionId?: string;
  /** Local context retains output reasoning and media references alongside the request Items. */
  context?: readonly ContextRecord[];
  nativeSpec?: ModelSpec;
  origin?: ItemOrigin;
  quote?: (at: QuoteTime) => readonly PriceSnapshot[];
  /** Diagnostic requests use a single attempt and cannot recursively diagnose. */
  diagnostic?: boolean;
}
export interface Generation {
  response: Response;
  origin: ItemOrigin;
  attempts: ProviderAttempt[];
}
export interface ResponseClient {
  respond(request: Request, options?: GenerateOptions): Promise<Generation>;
  /** A fork captures its provider binding once, before its first request. */
  bind?(): ResponseClient;
}
export class GenerationError extends Error {
  constructor(message: string, readonly attempts: ProviderAttempt[], readonly partial: Response | null,
    readonly origin: ItemOrigin, readonly status = 0, readonly body = '', options?: ErrorOptions) {
    super(message, options);
    this.name = 'GenerationError';
  }
}
export function unknownMeters(): TokenMeters {
  return { input: null, output: null, total: null, cachedInput: null, uncachedInput: null, reasoning: null, native: null };
}
export function standardUsage(meters: TokenMeters): Usage | null {
  const { input, output, total, cachedInput, reasoning } = meters;
  if (input === null || output === null || total === null || cachedInput === null || reasoning === null) return null;
  return { input_tokens: input, output_tokens: output, total_tokens: total,
    input_tokens_details: { cached_tokens: cachedInput }, output_tokens_details: { reasoning_tokens: reasoning } };
}
export function priceUsage(meters: TokenMeters, quotes: readonly PriceSnapshot[], serviceTier: string | null = null): Charge[] {
  return quotes.map(quote => {
    const missing: Charge['missing'] = [];
    let schedule: Pick<PriceSnapshot, 'rules' | 'inputBands'> = quote;
    if (quote.serviceTiers) {
      if (serviceTier === null || !quote.serviceTiers[serviceTier]) missing.push('serviceTier');
      else schedule = quote.serviceTiers[serviceTier];
    }
    let rules = schedule.rules;
    if (schedule.inputBands?.length) {
      if (meters.input === null) missing.push('input');
      else for (const band of schedule.inputBands) if (meters.input >= band.from) rules = band.rules;
    }
    const unresolvedSchedule = missing.length > 0;
    const lines = rules.map(rule => {
      const detail = rule.meter.startsWith('detail:') ? meters.details?.[rule.meter.slice(7)] : undefined;
      const quantity = rule.meter.startsWith('detail:') ? detail?.quantity ?? null : meters[rule.meter as Exclude<Meter, `detail:${string}`>];
      const unit = rule.unit ?? 'token';
      const known = !unresolvedSchedule && (!detail || detail.unit === unit);
      const amount = known && rule.perMillion === 0 ? 0 : known && quantity !== null ? quantity * rule.perMillion / 1e6 : null;
      if (amount === null && !unresolvedSchedule) missing.push(rule.meter);
      return { meter: rule.meter, unit, quantity, perMillion: rule.perMillion, amount };
    });
    const knownAmount = lines.reduce((sum, line) => sum + (line.amount ?? 0), 0);
    return { quote: structuredClone(quote), amount: missing.length ? null : knownAmount, knownAmount, missing: [...new Set(missing)], lines };
  });
}
