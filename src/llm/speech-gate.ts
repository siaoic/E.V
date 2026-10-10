export type SpeechIntent = { text: string };

export type GateResult = { accepted: true } | { accepted: false; reason: 'repetition' };

const normalize = (text: string) => text.replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase();

export class SpeechGate {
  check(intent: SpeechIntent, recentSentences: string[]): GateResult {
    const normalized = normalize(intent.text);
    if (recentSentences.some((sentence) => normalize(sentence) === normalized)) {
      return { accepted: false, reason: 'repetition' };
    }
    return { accepted: true };
  }
}
