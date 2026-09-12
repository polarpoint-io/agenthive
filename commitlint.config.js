// The package.json commitlint dependencies do nothing without this file, and
// semantic-release derives every version bump from these prefixes: fix ->
// patch, feat -> minor, a `!` or BREAKING CHANGE footer -> major. A commit
// that does not parse simply does not move the version, which is a silent
// failure worth catching at commit time instead.
module.exports = { extends: ["@commitlint/config-conventional"] };
