import ContactCard from '../components/thread/ContactCard';
import ReasoningPanel from '../components/thread/ReasoningPanel';
import RagContextPanel from '../components/thread/RagContextPanel';

const ThreadWorkspace = () => {
  return (
    <div className="grid grid-cols-12 gap-4 p-6 h-screen overflow-hidden">
      <div className="col-span-3 bg-white rounded shadow p-4 overflow-auto">
        <h2 className="text-xl font-semibold mb-4">Email Content</h2>
      </div>

      <div className="col-span-6 bg-white rounded shadow p-4 overflow-auto">
        <h2 className="text-xl font-semibold mb-4">Thread Timeline</h2>

        <div className="space-y-4">
          <div className="border rounded p-4">
            <p className="font-semibold">Customer</p>
            <p>Sample email content...</p>
          </div>
        </div>

        <div className="flex gap-3 mt-6">
          <button className="bg-green-600 text-white px-4 py-2 rounded">
            Approve & Send
          </button>

          <button className="bg-blue-600 text-white px-4 py-2 rounded">
            Edit Draft
          </button>

          <button className="bg-orange-600 text-white px-4 py-2 rounded">
            Escalate
          </button>

          <button className="bg-red-600 text-white px-4 py-2 rounded">
            Mark Spam
          </button>
        </div>
      </div>

      <div className="col-span-3 overflow-auto space-y-4">
        <ContactCard
          contact={{
            email: 'vip@client.com',
            status: 'VIP',
            is_vip: true,
            account_value: 12000,
            churn_risk_score: 0.8,
          }}
        />

        <ReasoningPanel
          reasoning={[
            {
              step: 'Thought',
              content: 'Customer sentiment deteriorating.',
            },
          ]}
        />

        <RagContextPanel
          chunks={[
            {
              source_doc: 'Refund Policy',
              similarity_score: 0.92,
              chunk_text: 'Refunds are processed within 5 business days.',
            },
          ]}
        />
      </div>
    </div>
  );
};

export default ThreadWorkspace;