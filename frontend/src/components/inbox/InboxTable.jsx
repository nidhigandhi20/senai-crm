import Badge from '../common/Badge';
import { formatDistanceToNow } from 'date-fns';

const InboxTable = ({ emails, onSelect }) => {
  return (
    <div className="bg-white rounded shadow overflow-hidden">
      <table className="w-full">
        <thead className="bg-gray-100">
          <tr>
            <th className="text-left p-3">Sender</th>
            <th className="text-left p-3">Subject</th>
            <th className="text-left p-3">Category</th>
            <th className="text-left p-3">Urgency</th>
            <th className="text-left p-3">Sentiment</th>
            <th className="text-left p-3">Last Activity</th>
          </tr>
        </thead>

        <tbody>
          {emails.map((email) => (
            <tr
              key={email.id}
              className="border-b hover:bg-gray-50 cursor-pointer"
              onClick={() => onSelect(email)}
            >
              <td className="p-3">{email.sender}</td>
              <td className="p-3">{email.subject}</td>
              <td className="p-3">{email.category}</td>
              <td className="p-3">
                <Badge label={email.urgency} />
              </td>
              <td className="p-3">
                <Badge
                  label={
                    email.sentiment_score > 0
                      ? 'Positive'
                      : email.sentiment_score < 0
                      ? 'Negative'
                      : 'Neutral'
                  }
                />
              </td>
              <td className="p-3">
                {formatDistanceToNow(new Date(email.timestamp))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export default InboxTable;