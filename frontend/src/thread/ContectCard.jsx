const ContactCard = ({ contact }) => {
  if (!contact) return null;

  return (
    <div className="bg-white rounded shadow p-4">
      <h2 className="text-xl font-semibold mb-4">Contact Profile</h2>

      <div className="space-y-2">
        <p><strong>Email:</strong> {contact.email}</p>
        <p><strong>Status:</strong> {contact.status}</p>
        <p><strong>VIP:</strong> {contact.is_vip ? 'Yes' : 'No'}</p>
        <p><strong>Account Value:</strong> ${contact.account_value || 0}</p>
        <p><strong>Churn Risk:</strong> {contact.churn_risk_score || 0}</p>
      </div>
    </div>
  );
};

export default ContactCard;