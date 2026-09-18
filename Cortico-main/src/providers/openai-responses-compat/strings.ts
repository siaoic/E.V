import { pick, type Language } from '../../core/language.ts';

/** Server side: ConfigGroup titles and validation errors. */
const zh = {
  endpointPath: 'Responses 端点路径',
  endpointPathDescription: '相对供应地址;默认 /responses。',
  endpointPathSlash: '端点路径必须以 / 开头',
  extraHeadersObject: '附加请求头必须是字符串到字符串的对象',
  extraBodyObject: '附加请求体字段必须是对象',
};
const en: typeof zh = {
  endpointPath: 'Responses endpoint path',
  endpointPathDescription: 'Relative to the provider URL; default /responses.',
  endpointPathSlash: 'The endpoint path must start with /',
  extraHeadersObject: 'Extra headers must be an object of strings',
  extraBodyObject: 'Extra body fields must be an object',
};
export type Text = typeof zh;
export const text = (language: Language) => pick(language, { zh, en });
