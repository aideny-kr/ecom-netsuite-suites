module.exports = {
    // SuiteCloud Unit Testing Framework stubs
    moduleNameMapper: {
        '^N/(.*)$': '<rootDir>/node_modules/@oracle/suitecloud-unit-testing/stubs/N/$1',
    },
    testMatch: ['**/__tests__/**/*.test.js'],
    testEnvironment: 'node',
    transform: {},
    verbose: true,
};
