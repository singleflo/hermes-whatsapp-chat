import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

export default [
  { ignores: ['.agents/**', '.venv/**', 'docs/**', 'sidecar/whatsapp-bridge/**'] },
  js.configs.recommended,
  {
    files: ['plugin/**/*.js'],
    languageOptions: { ecmaVersion: 'latest', globals: globals.browser },
    plugins: { 'react-hooks': reactHooks },
    rules: { curly: ['error', 'all'], 'react-hooks/exhaustive-deps': 'warn', 'react-hooks/rules-of-hooks': 'error' }
  },
  {
    files: ['plugin/desktop/plugin.js'],
    languageOptions: { sourceType: 'module' },
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: '^(?!(@hermes/plugin-sdk|react|react/jsx-runtime)$)',
              message: 'Desktop plugins can import only @hermes/plugin-sdk, react and react/jsx-runtime.'
            }
          ]
        }
      ]
    }
  },
  { files: ['plugin/dashboard/dist/index.js'], languageOptions: { sourceType: 'script' } },
  { files: ['eslint.config.mjs'], languageOptions: { globals: globals.node } }
]
