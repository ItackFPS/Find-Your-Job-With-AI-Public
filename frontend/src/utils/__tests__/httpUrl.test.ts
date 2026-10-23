import { describe, expect, it } from 'vitest'
import { httpUrl } from '../httpUrl'

describe('httpUrl', () => {
  it('keeps absolute http and https links', () => {
    expect(httpUrl('https://www.hellowork.com/fr-fr/emplois/1.html')).toBe('https://www.hellowork.com/fr-fr/emplois/1.html')
    expect(httpUrl(' http://example.org/a ')).toBe('http://example.org/a')
  })

  it('drops every other scheme, relative paths and garbage', () => {
    for (const value of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<b>x</b>', 'vbscript:x',
      'file:///C:/Windows/win.ini', 'ms-settings:privacy', '/api/jobs', 'not a url', '', null, undefined]) {
      expect(httpUrl(value)).toBeNull()
    }
  })
})
