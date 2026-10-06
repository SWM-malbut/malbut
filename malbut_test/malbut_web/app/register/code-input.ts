/** As typed: letters and digits only, upper case, 8 at most, with the dash added after the fourth. */
export function formatRegistrationCodeInput(value: string) {
  const compact = value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return compact.length > 4 ? `${compact.slice(0, 4)}-${compact.slice(4)}` : compact;
}
