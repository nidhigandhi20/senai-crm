const ReasoningPanel = ({ reasoning }) => {
  return (
    <div className="bg-white rounded shadow p-4 mt-4">
      <h2 className="text-lg font-semibold mb-3">Agent Reasoning</h2>

      <div className="space-y-3">
        {reasoning?.map((item, index) => (
          <div key={index} className="border-l-4 border-blue-500 pl-3">
            <p className="font-semibold">{item.step}</p>
            <p>{item.content}</p>
          </div>
        ))}
      </div>
    </div>
  );
};

export default ReasoningPanel;