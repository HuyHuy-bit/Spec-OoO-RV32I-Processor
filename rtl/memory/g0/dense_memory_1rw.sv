`default_nettype none
// g0 provider: only config/dense_memory.json g0_shapes elaborate.
module dense_memory_1rw #(
  parameter int DEPTH = 16,
  parameter int WIDTH = 128,
  parameter bit MASKED = 1'b1,
  localparam int LANES = MASKED ? WIDTH / 8 : 1,
  localparam int ADDR_BITS = $clog2(DEPTH)
) (
  input  wire                 clk_i,
  input  wire                 en_i,
  input  wire                 we_i,
  input  wire [ADDR_BITS-1:0] addr_i,
  input  wire [WIDTH-1:0]     wdata_i,
  input  wire [LANES-1:0]     wmask_i,
  output logic [WIDTH-1:0]    rdata_o
);
  if (!(DEPTH == 16 && ((WIDTH == 128 && MASKED) || (WIDTH == 23 && !MASKED)))) begin : g_not_whitelisted
    $error("dense_memory_1rw: shape not in G0 whitelist");
  end

  dense_memory_1rw_array #(.DEPTH(DEPTH), .WIDTH(WIDTH), .MASKED(MASKED)) storage (.*);
endmodule
`default_nettype wire
