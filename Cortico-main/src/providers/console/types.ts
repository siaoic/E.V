import type { LLMProviderEntry } from '../../core/types.ts';
import type { Language } from '../../core/language.ts';
import type { ProviderInstance } from '../base.ts';

export interface ProviderConsoleHost {
  /** Console language for panel titles, receipts and error texts. */
  readonly language: Language;
  entries(): Array<{ name: string; entry: LLMProviderEntry }>;
  instance(name: string): ProviderInstance;
  save(name: string, entry: LLMProviderEntry): void;
}
