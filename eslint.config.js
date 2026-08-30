// ESLint flat config (ESLint 9+ replaced .eslintrc.json with this format).
// Rules here are a 1:1 port of the previous .eslintrc.json — same recommended
// TypeScript set, same four overrides — so the upgrade changed the config
// format, not what gets flagged.
const tseslint = require('typescript-eslint');

module.exports = tseslint.config(
  // Build output isn't linted. Flat config has no .eslintignore; ignores live
  // here instead.
  { ignores: ['dist/**'] },

  ...tseslint.configs.recommended,

  {
    files: ['src/**/*.ts'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      // Unused function args are fine when prefixed with _ (HomeKit callbacks
      // often take args we don't use).
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      quotes: ['error', 'single'],
      semi: ['error', 'always'],
    },
  },
);
